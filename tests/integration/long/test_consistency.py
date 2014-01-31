import struct
import traceback

import cassandra
from cassandra import ConsistencyLevel
from cassandra.cluster import Cluster
from cassandra.policies import TokenAwarePolicy, RoundRobinPolicy, \
    DowngradingConsistencyRetryPolicy
from cassandra.query import SimpleStatement

from tests.integration.long.utils import force_stop, create_schema, \
    wait_for_down, wait_for_up, start, CoordinatorStats

try:
    import unittest2 as unittest
except ImportError:
    import unittest # noqa

ALL_CONSISTENCY_LEVELS = set([
    ConsistencyLevel.ANY, ConsistencyLevel.ONE, ConsistencyLevel.TWO,
    ConsistencyLevel.QUORUM, ConsistencyLevel.THREE,
    ConsistencyLevel.ALL, ConsistencyLevel.LOCAL_QUORUM,
    ConsistencyLevel.EACH_QUORUM])

MULTI_DC_CONSISTENCY_LEVELS = set([
    ConsistencyLevel.LOCAL_QUORUM, ConsistencyLevel.EACH_QUORUM])

SINGLE_DC_CONSISTENCY_LEVELS = ALL_CONSISTENCY_LEVELS - MULTI_DC_CONSISTENCY_LEVELS


class ConsistencyTests(unittest.TestCase):

    def setUp(self):
        self.coordinator_stats = CoordinatorStats()

    def _cl_failure(self, consistency_level, e):
        self.fail('Instead of success, saw %s for CL.%s:\n\n%s' % (
            e, ConsistencyLevel.value_to_name[consistency_level],
            traceback.format_exc()))

    def _cl_expected_failure(self, cl):
        self.fail('Test passed at ConsistencyLevel.%s:\n\n%s' % (
                  ConsistencyLevel.value_to_name[cl], traceback.format_exc()))

    def _insert(self, session, keyspace, count, consistency_level=ConsistencyLevel.ONE):
        session.execute('USE %s' % keyspace)
        for i in range(count):
            ss = SimpleStatement('INSERT INTO cf(k, i) VALUES (0, 0)',
                                 consistency_level=consistency_level)
            session.execute(ss)

    def _query(self, session, keyspace, count, consistency_level=ConsistencyLevel.ONE):
        routing_key = struct.pack('>i', 0)
        for i in range(count):
            ss = SimpleStatement('SELECT * FROM cf WHERE k = 0',
                                 consistency_level=consistency_level,
                                 routing_key=routing_key)
            self.coordinator_stats.add_coordinator(session.execute_async(ss))

    def _assert_writes_succeed(self, session, keyspace, consistency_levels):
        for cl in consistency_levels:
            self.coordinator_stats.reset_counts()
            try:
                self._insert(session, keyspace, 1, cl)
            except Exception as e:
                self._cl_failure(cl, e)

    def _assert_reads_succeed(self, session, keyspace, consistency_levels, expected_reader=3):
        for cl in consistency_levels:
            self.coordinator_stats.reset_counts()
            try:
                self._query(session, keyspace, 1, cl)
                for i in range(3):
                    if i == expected_reader:
                        self.coordinator_stats.assert_query_count_equals(self, i, 1)
                    else:
                        self.coordinator_stats.assert_query_count_equals(self, i, 0)
            except Exception as e:
                self._cl_failure(cl, e)

    def _assert_writes_fail(self, session, keyspace, consistency_levels):
        for cl in consistency_levels:
            self.coordinator_stats.reset_counts()
            try:
                self._insert(session, keyspace, 1, cl)
                self._cl_expected_failure(cl)
            except (cassandra.Unavailable, cassandra.WriteTimeout):
                pass

    def _assert_reads_fail(self, session, keyspace, consistency_levels):
        for cl in consistency_levels:
            self.coordinator_stats.reset_counts()
            try:
                self._query(session, keyspace, 1, cl)
                self._cl_expected_failure(cl)
            except (cassandra.Unavailable, cassandra.ReadTimeout):
                pass

    def _test_tokenaware_one_node_down(self, keyspace, rf, accepted):
        cluster = Cluster(
            load_balancing_policy=TokenAwarePolicy(RoundRobinPolicy()))
        session = cluster.connect()
        wait_for_up(cluster, 1, wait=False)
        wait_for_up(cluster, 2)

        create_schema(session, keyspace, replication_factor=rf)
        self._insert(session, keyspace, count=1)
        self._query(session, keyspace, count=1)
        self.coordinator_stats.assert_query_count_equals(self, 1, 0)
        self.coordinator_stats.assert_query_count_equals(self, 2, 1)
        self.coordinator_stats.assert_query_count_equals(self, 3, 0)

        try:
            force_stop(2)
            wait_for_down(cluster, 2)

            self._assert_writes_succeed(session, keyspace, accepted)
            self._assert_reads_succeed(session, keyspace,
                    accepted - set([ConsistencyLevel.ANY]))
            self._assert_writes_fail(session, keyspace,
                    SINGLE_DC_CONSISTENCY_LEVELS - accepted)
            self._assert_reads_fail(session, keyspace,
                    SINGLE_DC_CONSISTENCY_LEVELS - accepted)
        finally:
            start(2)
            wait_for_up(cluster, 2)

    def test_rfone_tokenaware_one_node_down(self):
        self._test_tokenaware_one_node_down(
            keyspace='test_rfone_tokenaware',
            rf=1,
            accepted=set([ConsistencyLevel.ANY]))

    def test_rftwo_tokenaware_one_node_down(self):
        self._test_tokenaware_one_node_down(
            keyspace='test_rftwo_tokenaware',
            rf=2,
            accepted=set([ConsistencyLevel.ANY, ConsistencyLevel.ONE]))

    def test_rfthree_tokenaware_one_node_down(self):
        self._test_tokenaware_one_node_down(
            keyspace='test_rfthree_tokenaware',
            rf=3,
            accepted=set([ConsistencyLevel.ANY, ConsistencyLevel.ONE,
                          ConsistencyLevel.TWO, ConsistencyLevel.QUORUM]))

    def test_rfthree_tokenaware_none_down(self):
        keyspace = 'test_rfthree_tokenaware_none_down'
        cluster = Cluster(
            load_balancing_policy=TokenAwarePolicy(RoundRobinPolicy()))
        session = cluster.connect()
        wait_for_up(cluster, 1, wait=False)
        wait_for_up(cluster, 2)

        create_schema(session, keyspace, replication_factor=3)
        self._insert(session, keyspace, count=1)
        self._query(session, keyspace, count=1)
        self.coordinator_stats.assert_query_count_equals(self, 1, 0)
        self.coordinator_stats.assert_query_count_equals(self, 2, 1)
        self.coordinator_stats.assert_query_count_equals(self, 3, 0)

        self.coordinator_stats.reset_counts()

        self._assert_writes_succeed(session, keyspace, SINGLE_DC_CONSISTENCY_LEVELS)
        self._assert_reads_succeed(session, keyspace,
                SINGLE_DC_CONSISTENCY_LEVELS - set([ConsistencyLevel.ANY]),
                expected_reader=2)

    def _test_downgrading_cl(self, keyspace, rf, accepted):
        cluster = Cluster(
            load_balancing_policy=TokenAwarePolicy(RoundRobinPolicy()),
            default_retry_policy=DowngradingConsistencyRetryPolicy())
        session = cluster.connect()

        create_schema(session, keyspace, replication_factor=rf)
        self._insert(session, keyspace, 1)
        self._query(session, keyspace, 1)
        self.coordinator_stats.assert_query_count_equals(self, 1, 0)
        self.coordinator_stats.assert_query_count_equals(self, 2, 1)
        self.coordinator_stats.assert_query_count_equals(self, 3, 0)

        try:
            force_stop(2)
            wait_for_down(cluster, 2)

            self._assert_writes_succeed(session, keyspace, accepted)
            self._assert_reads_succeed(session, keyspace,
                    accepted - set([ConsistencyLevel.ANY]))
            self._assert_writes_fail(session, keyspace,
                    SINGLE_DC_CONSISTENCY_LEVELS - accepted)
            self._assert_reads_fail(session, keyspace,
                    SINGLE_DC_CONSISTENCY_LEVELS - accepted)
        finally:
            start(2)
            wait_for_up(cluster, 2)

    def test_rfone_downgradingcl(self):
        self._test_downgrading_cl(
            keyspace='test_rfone_downgradingcl',
            rf=1,
            accepted=set([ConsistencyLevel.ANY]))

    def test_rftwo_downgradingcl(self):
        self._test_downgrading_cl(
            keyspace='test_rftwo_downgradingcl',
            rf=2,
            accepted=SINGLE_DC_CONSISTENCY_LEVELS)

    def test_rfthree_roundrobin_downgradingcl(self):
        keyspace = 'test_rfthree_roundrobin_downgradingcl'
        cluster = Cluster(
            load_balancing_policy=RoundRobinPolicy(),
            default_retry_policy=DowngradingConsistencyRetryPolicy())
        self.rfthree_downgradingcl(cluster, keyspace, True)

    def test_rfthree_tokenaware_downgradingcl(self):
        keyspace = 'test_rfthree_tokenaware_downgradingcl'
        cluster = Cluster(
            load_balancing_policy=TokenAwarePolicy(RoundRobinPolicy()),
            default_retry_policy=DowngradingConsistencyRetryPolicy())
        self.rfthree_downgradingcl(cluster, keyspace, False)

    def rfthree_downgradingcl(self, cluster, keyspace, roundrobin):
        session = cluster.connect()

        create_schema(session, keyspace, replication_factor=2)
        self._insert(session, keyspace, count=12)
        self._query(session, keyspace, count=12)

        if roundrobin:
            self.coordinator_stats.assert_query_count_equals(self, 1, 4)
            self.coordinator_stats.assert_query_count_equals(self, 2, 4)
            self.coordinator_stats.assert_query_count_equals(self, 3, 4)
        else:
            self.coordinator_stats.assert_query_count_equals(self, 1, 0)
            self.coordinator_stats.assert_query_count_equals(self, 2, 12)
            self.coordinator_stats.assert_query_count_equals(self, 3, 0)

        try:
            self.coordinator_stats.reset_counts()
            force_stop(2)
            wait_for_down(cluster, 2)

            self._assert_writes_succeed(session, keyspace, SINGLE_DC_CONSISTENCY_LEVELS)

            # Test reads that expected to complete successfully
            for cl in SINGLE_DC_CONSISTENCY_LEVELS - set([ConsistencyLevel.ANY]):
                self.coordinator_stats.reset_counts()
                self._query(session, keyspace, 12, consistency_level=cl)
                if roundrobin:
                    self.coordinator_stats.assert_query_count_equals(self, 1, 6)
                    self.coordinator_stats.assert_query_count_equals(self, 2, 0)
                    self.coordinator_stats.assert_query_count_equals(self, 3, 6)
                else:
                    self.coordinator_stats.assert_query_count_equals(self, 1, 0)
                    self.coordinator_stats.assert_query_count_equals(self, 2, 0)
                    self.coordinator_stats.assert_query_count_equals(self, 3, 12)
        finally:
            start(2)
            wait_for_up(cluster, 2)

    # TODO: can't be done in this class since we reuse the ccm cluster
    #       instead we should create these elsewhere
    # def test_rfthree_downgradingcl_twodcs(self):
    # def test_rfthree_downgradingcl_twodcs_dcaware(self):