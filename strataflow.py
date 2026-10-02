"""StrataFlow: incremental dependency-edge resolution backed by SQLite.

Nodes are identified by (hostname, primary_ip) and carry a full address set
(including primary_ip) plus a full set of outbound connection declarations.
Edges are resolved from declarations: a declaration yields an edge iff exactly
one live node owns its target_ip. Declarations with zero or multiple candidate
targets are kept in storage and re-resolved on every subsequent batch.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, NamedTuple, Set, Tuple


class Declaration(NamedTuple):
    """An outbound connection declaration: (target_ip, protocol, port)."""

    target_ip: str
    protocol: str
    port: int


class NodeIdentity(NamedTuple):
    """Immutable node identity: (hostname, primary_ip)."""

    hostname: str
    primary_ip: str


class Edge(NamedTuple):
    """A resolved dependency edge between two node identities."""

    source: NodeIdentity
    target: NodeIdentity
    protocol: str
    port: int


class Node(NamedTuple):
    """Full node state used for upserts."""

    identity: NodeIdentity
    addresses: frozenset  # frozenset[str], must contain identity.primary_ip
    declarations: frozenset  # frozenset[Declaration]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(
    hostname TEXT NOT NULL,
    primary_ip TEXT NOT NULL,
    PRIMARY KEY(hostname, primary_ip)
);
CREATE TABLE IF NOT EXISTS node_addresses(
    hostname TEXT NOT NULL,
    primary_ip TEXT NOT NULL,
    address TEXT NOT NULL,
    PRIMARY KEY(hostname, primary_ip, address)
);
CREATE TABLE IF NOT EXISTS declarations(
    src_hostname TEXT NOT NULL,
    src_primary_ip TEXT NOT NULL,
    target_ip TEXT NOT NULL,
    protocol TEXT NOT NULL,
    port INTEGER NOT NULL,
    PRIMARY KEY(src_hostname, src_primary_ip, target_ip, protocol, port)
);
CREATE TABLE IF NOT EXISTS edges(
    src_hostname TEXT NOT NULL,
    src_primary_ip TEXT NOT NULL,
    dst_hostname TEXT NOT NULL,
    dst_primary_ip TEXT NOT NULL,
    protocol TEXT NOT NULL,
    port INTEGER NOT NULL,
    PRIMARY KEY(
        src_hostname, src_primary_ip, dst_hostname, dst_primary_ip,
        protocol, port
    )
);
"""


class StrataFlow:
    """Dependency-edge resolver persisted in a SQLite database."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def apply_batch(
        self,
        upserts: Iterable[Node] = (),
        deletes: Iterable[NodeIdentity] = (),
    ) -> Tuple[Set[Edge], Set[Edge]]:
        """Atomically apply a batch; return (added_edges, removed_edges).

        Upserts fully replace each node's address set and declarations.
        Deletes remove the node and its outbound declarations, but never
        other nodes' declarations that point at the deleted node's addresses.
        The returned sets are the net difference of the edge set before and
        after the batch.
        """
        upserts = list(upserts)
        deletes = [NodeIdentity(*d) for d in deletes]
        for node in upserts:
            if node.identity.primary_ip not in node.addresses:
                raise ValueError(
                    f"addresses of {node.identity!r} must contain its primary_ip"
                )

        conn = self._conn
        try:
            conn.execute("BEGIN")
            for ident in deletes:
                self._delete_node(ident)
            for node in upserts:
                self._upsert_node(node)
            new_edges = self._resolve_all()
            old_edges = self._stored_edges()
            added = new_edges - old_edges
            removed = old_edges - new_edges
            self._replace_edges(new_edges)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return added, removed

    def read_graph(self) -> Set[Edge]:
        """Return the current resolved edge set."""
        return self._stored_edges()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StrataFlow":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _delete_node(self, ident: NodeIdentity) -> None:
        cur = self._conn
        cur.execute(
            "DELETE FROM declarations WHERE src_hostname=? AND src_primary_ip=?",
            ident,
        )
        cur.execute(
            "DELETE FROM node_addresses WHERE hostname=? AND primary_ip=?",
            ident,
        )
        cur.execute(
            "DELETE FROM nodes WHERE hostname=? AND primary_ip=?",
            ident,
        )

    def _upsert_node(self, node: Node) -> None:
        ident = node.identity
        self._delete_node(ident)
        cur = self._conn
        cur.execute(
            "INSERT INTO nodes(hostname, primary_ip) VALUES (?, ?)", ident
        )
        cur.executemany(
            "INSERT INTO node_addresses(hostname, primary_ip, address)"
            " VALUES (?, ?, ?)",
            [(ident.hostname, ident.primary_ip, addr) for addr in node.addresses],
        )
        cur.executemany(
            "INSERT INTO declarations"
            "(src_hostname, src_primary_ip, target_ip, protocol, port)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (ident.hostname, ident.primary_ip, d.target_ip, d.protocol, d.port)
                for d in node.declarations
            ],
        )

    def _resolve_all(self) -> Set[Edge]:
        """Full re-resolution over all live nodes and saved declarations."""
        addr_to_nodes = {}
        for hostname, primary_ip, address in self._conn.execute(
            "SELECT hostname, primary_ip, address FROM node_addresses"
        ):
            addr_to_nodes.setdefault(address, set()).add((hostname, primary_ip))

        edges: Set[Edge] = set()
        for src_h, src_ip, target_ip, protocol, port in self._conn.execute(
            "SELECT src_hostname, src_primary_ip, target_ip, protocol, port"
            " FROM declarations"
        ):
            candidates = addr_to_nodes.get(target_ip, ())
            if len(candidates) == 1:
                dst_h, dst_ip = next(iter(candidates))
                edges.add(
                    Edge(
                        NodeIdentity(src_h, src_ip),
                        NodeIdentity(dst_h, dst_ip),
                        protocol,
                        port,
                    )
                )
        return edges

    def _stored_edges(self) -> Set[Edge]:
        edges: Set[Edge] = set()
        for row in self._conn.execute(
            "SELECT src_hostname, src_primary_ip, dst_hostname, dst_primary_ip,"
            " protocol, port FROM edges"
        ):
            src_h, src_ip, dst_h, dst_ip, protocol, port = row
            edges.add(
                Edge(
                    NodeIdentity(src_h, src_ip),
                    NodeIdentity(dst_h, dst_ip),
                    protocol,
                    port,
                )
            )
        return edges

    def _replace_edges(self, edges: Set[Edge]) -> None:
        self._conn.execute("DELETE FROM edges")
        self._conn.executemany(
            "INSERT INTO edges"
            "(src_hostname, src_primary_ip, dst_hostname, dst_primary_ip,"
            " protocol, port) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    e.source.hostname,
                    e.source.primary_ip,
                    e.target.hostname,
                    e.target.primary_ip,
                    e.protocol,
                    e.port,
                )
                for e in edges
            ],
        )
