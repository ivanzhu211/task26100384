import os
import tempfile
import unittest

from strataflow import Node, StrataFlow


def node(host, ip, addresses=(), decls=()):
    return Node(host, ip, addresses=addresses, declarations=decls)


class TestStrataFlow(unittest.TestCase):
    def setUp(self):
        self.sf = StrataFlow()

    def tearDown(self):
        self.sf.close()

    def test_two_target_ips_support_same_edge(self):
        # Node B owns 10.0.0.2 and 10.0.0.3; A declares both IPs with the
        # same protocol/port, producing a single edge A->B.
        b = node("b", "10.0.0.2", addresses={"10.0.0.2", "10.0.0.3"})
        a = node("a", "10.0.0.1", decls={
            ("10.0.0.2", "tcp", 443),
            ("10.0.0.3", "tcp", 443),
        })
        added, removed = self.sf.apply_batch(upserts=[a, b])
        edge = (("a", "10.0.0.1"), ("b", "10.0.0.2"), "tcp", 443)
        self.assertEqual(added, {edge})
        self.assertEqual(removed, set())
        self.assertEqual(self.sf.read_graph(), {edge})

        # Drop one of the two declarations: edge still supported, no events.
        a2 = node("a", "10.0.0.1", decls={("10.0.0.2", "tcp", 443)})
        added, removed = self.sf.apply_batch(upserts=[a2])
        self.assertEqual(added, set())
        self.assertEqual(removed, set())
        self.assertEqual(self.sf.read_graph(), {edge})

        # Drop the last supporting declaration: edge removed.
        a3 = node("a", "10.0.0.1", decls=set())
        added, removed = self.sf.apply_batch(upserts=[a3])
        self.assertEqual(added, set())
        self.assertEqual(removed, {edge})
        self.assertEqual(self.sf.read_graph(), set())

    def test_address_moved_between_nodes_same_batch(self):
        # S declares 10.0.0.9 which initially belongs to A.
        s = node("s", "10.0.0.1", decls={("10.0.0.9", "tcp", 80)})
        a = node("a", "10.0.0.8", addresses={"10.0.0.8", "10.0.0.9"})
        added, _ = self.sf.apply_batch(upserts=[s, a])
        edge_sa = (("s", "10.0.0.1"), ("a", "10.0.0.8"), "tcp", 80)
        self.assertEqual(added, {edge_sa})

        # Same batch: address moves A -> B, source S untouched.
        a2 = node("a", "10.0.0.8")          # A no longer owns 10.0.0.9
        b = node("b", "10.0.0.9")           # B now owns it
        added, removed = self.sf.apply_batch(upserts=[a2, b])
        edge_sb = (("s", "10.0.0.1"), ("b", "10.0.0.9"), "tcp", 80)
        self.assertEqual(added, {edge_sb})
        self.assertEqual(removed, {edge_sa})
        self.assertEqual(self.sf.read_graph(), {edge_sb})

    def test_unique_to_ambiguous_and_back(self):
        s = node("s", "10.0.0.1", decls={("10.0.0.5", "tcp", 22)})
        a = node("a", "10.0.0.5")
        added, _ = self.sf.apply_batch(upserts=[s, a])
        edge_sa = (("s", "10.0.0.1"), ("a", "10.0.0.5"), "tcp", 22)
        self.assertEqual(added, {edge_sa})

        # B also claims 10.0.0.5 -> ambiguous, edge disappears.
        b = node("b", "10.0.0.2", addresses={"10.0.0.2", "10.0.0.5"})
        added, removed = self.sf.apply_batch(upserts=[b])
        self.assertEqual(added, set())
        self.assertEqual(removed, {edge_sa})
        self.assertEqual(self.sf.read_graph(), set())

        # B releases the address -> unique again, edge restored.
        b2 = node("b", "10.0.0.2")
        added, removed = self.sf.apply_batch(upserts=[b2])
        self.assertEqual(added, {edge_sa})
        self.assertEqual(removed, set())
        self.assertEqual(self.sf.read_graph(), {edge_sa})

    def test_self_loop_allowed(self):
        a = node("a", "10.0.0.1", decls={("10.0.0.1", "tcp", 1)})
        added, _ = self.sf.apply_batch(upserts=[a])
        loop = (("a", "10.0.0.1"), ("a", "10.0.0.1"), "tcp", 1)
        self.assertEqual(added, {loop})
        self.assertEqual(self.sf.read_graph(), {loop})

    def test_delete_keeps_inbound_declarations(self):
        s = node("s", "10.0.0.1", decls={("10.0.0.2", "tcp", 80)})
        b = node("b", "10.0.0.2")
        self.sf.apply_batch(upserts=[s, b])
        added, removed = self.sf.apply_batch(deletes=[("b", "10.0.0.2")])
        edge = (("s", "10.0.0.1"), ("b", "10.0.0.2"), "tcp", 80)
        self.assertEqual(removed, {edge})
        self.assertEqual(self.sf.read_graph(), set())
        # Declaration on S survives; recreating B restores the edge.
        added, removed = self.sf.apply_batch(upserts=[node("b", "10.0.0.2")])
        self.assertEqual(added, {edge})
        self.assertEqual(removed, set())

    def test_transient_edge_in_batch_produces_no_event(self):
        # A declaration added and its target deleted within one batch must
        # yield no net events.
        s = node("s", "10.0.0.1", decls={("10.0.0.2", "tcp", 80)})
        b = node("b", "10.0.0.2")
        added, removed = self.sf.apply_batch(
            upserts=[s], deletes=[("b", "10.0.0.2")]
        )
        self.assertEqual(added, set())
        self.assertEqual(removed, set())
        self.assertEqual(self.sf.read_graph(), set())

    def test_atomic_rollback_on_invalid_batch(self):
        s = node("s", "10.0.0.1", decls={("10.0.0.2", "tcp", 80)})
        b = node("b", "10.0.0.2")
        self.sf.apply_batch(upserts=[s, b])
        before = self.sf.read_graph()
        # Port of wrong type triggers a sqlite error mid-batch.
        bad = node("x", "10.0.0.9", decls={("10.0.0.1", "tcp", "notaport")})
        with self.assertRaises(Exception):
            self.sf.apply_batch(upserts=[bad])
        self.assertEqual(self.sf.read_graph(), before)


class TestPersistence(unittest.TestCase):
    def test_unresolved_declaration_resolves_after_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "graph.db")
            sf = StrataFlow(path)
            # Declaration with no matching target: stored, no edge.
            s = node("s", "10.0.0.1", decls={("10.0.0.7", "tcp", 53)})
            added, removed = sf.apply_batch(upserts=[s])
            self.assertEqual((added, removed), (set(), set()))
            self.assertEqual(sf.read_graph(), set())
            sf.close()

            # Reopen; the missing target appears only now.
            sf = StrataFlow(path)
            self.assertEqual(sf.read_graph(), set())
            b = node("b", "10.0.0.7")
            added, removed = sf.apply_batch(upserts=[b])
            edge = (("s", "10.0.0.1"), ("b", "10.0.0.7"), "tcp", 53)
            self.assertEqual(added, {edge})
            self.assertEqual(removed, set())
            self.assertEqual(sf.read_graph(), {edge})
            sf.close()

    def test_ambiguity_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "graph.db")
            sf = StrataFlow(path)
            s = node("s", "10.0.0.1", decls={("10.0.0.5", "tcp", 22)})
            a = node("a", "10.0.0.5")
            b = node("b", "10.0.0.2", addresses={"10.0.0.2", "10.0.0.5"})
            sf.apply_batch(upserts=[s, a, b])
            self.assertEqual(sf.read_graph(), set())  # ambiguous
            sf.close()

            sf = StrataFlow(path)
            self.assertEqual(sf.read_graph(), set())
            # Resolve ambiguity after reopen.
            b2 = node("b", "10.0.0.2")
            added, _ = sf.apply_batch(upserts=[b2])
            edge = (("s", "10.0.0.1"), ("a", "10.0.0.5"), "tcp", 22)
            self.assertEqual(added, {edge})
            sf.close()


if __name__ == "__main__":
    unittest.main()
