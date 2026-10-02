"""Tests for strataflow."""

import os
import tempfile
import unittest

from strataflow import (
    Declaration,
    Edge,
    Node,
    NodeIdentity,
    StrataFlow,
)


def ident(name, ip):
    return NodeIdentity(name, ip)


def node(name, ip, extra_addrs=(), decls=()):
    return Node(
        ident(name, ip),
        frozenset({ip, *extra_addrs}),
        frozenset(decls),
    )


def decl(ip, proto="tcp", port=80):
    return Declaration(ip, proto, port)


def edge(src, dst, proto="tcp", port=80):
    return Edge(src, dst, proto, port)


class StrataFlowTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.sf = StrataFlow(self.db_path)

    def tearDown(self):
        self.sf.close()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def reopen(self):
        self.sf.close()
        self.sf = StrataFlow(self.db_path)


class TestMultiDeclarationSupport(StrataFlowTestCase):
    def test_two_target_ips_support_same_edge(self):
        a = ident("a", "10.0.0.1")
        b = ident("b", "10.0.0.2")
        # b owns two addresses; a declares both with same protocol/port.
        added, _ = self.sf.apply_batch([
            node("a", "10.0.0.1", decls=[decl("10.0.0.2"), decl("10.0.0.22")]),
            node("b", "10.0.0.2", extra_addrs=["10.0.0.22"]),
        ])
        self.assertEqual(added, {edge(a, b)})
        self.assertEqual(self.sf.read_graph(), {edge(a, b)})

        # Remove one of the two supporting declarations: edge must survive
        # and no removal event may be reported.
        added, removed = self.sf.apply_batch([
            node("a", "10.0.0.1", decls=[decl("10.0.0.2")]),
        ])
        self.assertEqual(added, set())
        self.assertEqual(removed, set())
        self.assertEqual(self.sf.read_graph(), {edge(a, b)})


class TestAddressTransfer(StrataFlowTestCase):
    def test_move_address_between_nodes_in_one_batch(self):
        a = ident("a", "10.0.0.1")
        host_a = ident("A", "10.0.0.2")
        host_b = ident("B", "10.0.0.3")
        moving = "10.0.0.9"

        added, _ = self.sf.apply_batch([
            node("a", "10.0.0.1", decls=[decl(moving, "tcp", 443)]),
            node("A", "10.0.0.2", extra_addrs=[moving]),
            node("B", "10.0.0.3"),
        ])
        self.assertEqual(added, {edge(a, host_a, "tcp", 443)})

        # Move the address A -> B in a single batch; source node `a` is not
        # part of the batch but its edge must be re-bound.
        added, removed = self.sf.apply_batch([
            node("A", "10.0.0.2"),
            node("B", "10.0.0.3", extra_addrs=[moving]),
        ])
        self.assertEqual(added, {edge(a, host_b, "tcp", 443)})
        self.assertEqual(removed, {edge(a, host_a, "tcp", 443)})
        self.assertEqual(self.sf.read_graph(), {edge(a, host_b, "tcp", 443)})


class TestAmbiguity(StrataFlowTestCase):
    def test_unique_to_ambiguous_and_back(self):
        s = ident("s", "10.0.0.1")
        t1 = ident("t1", "10.0.0.2")
        t2 = ident("t2", "10.0.0.3")
        shared = "10.0.0.5"

        added, _ = self.sf.apply_batch([
            node("s", "10.0.0.1", decls=[decl(shared, "tcp", 22)]),
            node("t1", "10.0.0.2", extra_addrs=[shared]),
        ])
        self.assertEqual(added, {edge(s, t1, "tcp", 22)})

        # Second node claims the same address: edge becomes ambiguous.
        added, removed = self.sf.apply_batch([
            node("t2", "10.0.0.3", extra_addrs=[shared]),
        ])
        self.assertEqual(added, set())
        self.assertEqual(removed, {edge(s, t1, "tcp", 22)})
        self.assertEqual(self.sf.read_graph(), set())

        # Ambiguity resolved: the original edge comes back.
        added, removed = self.sf.apply_batch([
            node("t2", "10.0.0.3"),
        ])
        self.assertEqual(added, {edge(s, t1, "tcp", 22)})
        self.assertEqual(removed, set())
        self.assertEqual(self.sf.read_graph(), {edge(s, t1, "tcp", 22)})


class TestPersistence(StrataFlowTestCase):
    def test_missing_target_resolves_after_reopen(self):
        c = ident("c", "10.0.0.1")
        d = ident("d", "10.0.0.7")

        # Target does not exist yet: no edge, but the declaration is saved.
        added, removed = self.sf.apply_batch([
            node("c", "10.0.0.1", decls=[decl("10.0.0.7", "tcp", 8080)]),
        ])
        self.assertEqual(added, set())
        self.assertEqual(removed, set())
        self.assertEqual(self.sf.read_graph(), set())

        self.reopen()

        # After reopening the database the saved declaration re-resolves.
        added, removed = self.sf.apply_batch([
            node("d", "10.0.0.7"),
        ])
        self.assertEqual(added, {edge(c, d, "tcp", 8080)})
        self.assertEqual(removed, set())
        self.assertEqual(self.sf.read_graph(), {edge(c, d, "tcp", 8080)})

    def test_ambiguous_declaration_resolves_after_reopen(self):
        s = ident("s", "10.0.0.1")
        t1 = ident("t1", "10.0.0.2")
        shared = "10.0.0.5"

        self.sf.apply_batch([
            node("s", "10.0.0.1", decls=[decl(shared)]),
            node("t1", "10.0.0.2", extra_addrs=[shared]),
            node("t2", "10.0.0.3", extra_addrs=[shared]),
        ])
        self.assertEqual(self.sf.read_graph(), set())

        self.reopen()

        added, removed = self.sf.apply_batch([], deletes=[ident("t2", "10.0.0.3")])
        self.assertEqual(added, {edge(s, t1)})
        self.assertEqual(removed, set())


class TestDeleteSemantics(StrataFlowTestCase):
    def test_delete_keeps_other_nodes_declarations(self):
        a = ident("a", "10.0.0.1")
        b = ident("b", "10.0.0.2")

        self.sf.apply_batch([
            node("a", "10.0.0.1", decls=[decl("10.0.0.2")]),
            node("b", "10.0.0.2"),
        ])
        self.assertEqual(self.sf.read_graph(), {edge(a, b)})

        # Deleting b removes the edge but not a's declaration.
        added, removed = self.sf.apply_batch([], deletes=[b])
        self.assertEqual(added, set())
        self.assertEqual(removed, {edge(a, b)})
        self.assertEqual(self.sf.read_graph(), set())

        # Re-adding b revives the edge from the saved declaration.
        added, removed = self.sf.apply_batch([node("b", "10.0.0.2")])
        self.assertEqual(added, {edge(a, b)})
        self.assertEqual(removed, set())

    def test_self_loop(self):
        a = ident("a", "10.0.0.1")
        added, _ = self.sf.apply_batch([
            node("a", "10.0.0.1", decls=[decl("10.0.0.1", "tcp", 9000)]),
        ])
        self.assertEqual(added, {edge(a, a, "tcp", 9000)})
        self.assertEqual(self.sf.read_graph(), {edge(a, a, "tcp", 9000)})


class TestAtomicity(StrataFlowTestCase):
    def test_invalid_upsert_rejected_and_state_unchanged(self):
        a = ident("a", "10.0.0.1")
        self.sf.apply_batch([node("a", "10.0.0.1")])
        bad = Node(ident("bad", "10.0.0.9"), frozenset({"10.0.0.10"}), frozenset())
        with self.assertRaises(ValueError):
            self.sf.apply_batch([bad], deletes=[a])
        # Neither the delete nor the upsert was applied.
        self.assertEqual(
            self.sf.read_graph(),
            set(),
        )
        self.reopen()
        # Node a still exists: deleting it now is a no-op on edges but the
        # node itself must have survived the failed batch.
        self.sf.apply_batch([
            node("x", "10.0.0.1", decls=[decl("10.0.0.1")]),
        ])
        # a (10.0.0.1) and x (10.0.0.1) both own the address -> ambiguous.
        self.assertEqual(self.sf.read_graph(), set())


if __name__ == "__main__":
    unittest.main()
