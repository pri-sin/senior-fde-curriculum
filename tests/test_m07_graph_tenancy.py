"""Module 07 regressions: tenant-scoped nodes, legal-suffix normalisation, evidence in the Cypher result."""
import asyncio

import pytest

from _labs import load

m = load("07_graph_rag")


def _run(coro):
    return asyncio.run(coro)


def test_spellings_of_one_company_resolve_to_one_node():
    r = m.EntityResolver("tA")
    ids = {r.resolve(m.Entity(name=n, type="Company")) for n in ("Borealis", "Borealis Ltd", "Borealis Limited", "Borealis, Inc.")}
    assert ids == {"tA|Company:borealis"}


def test_same_name_under_two_tenants_is_two_nodes():
    a = m.EntityResolver("tA").resolve(m.Entity(name="Acme", type="Company"))
    b = m.EntityResolver("tB").resolve(m.Entity(name="Acme", type="Company"))
    assert a != b


def test_resolver_requires_a_tenant():
    with pytest.raises(m.GraphError):
        m.EntityResolver("")


def test_cross_tenant_read_fails_closed():
    async def go():
        g = m.InMemoryGraph()
        await m.ingest("doc", g, tenant="tA")
        cypher, params = m.CypherPlanner().plan("tA|Company:acme", ["OWNS"], 2, 5, "tB")  # tB asks about tA's seed
        return await g.read(cypher, params)

    with pytest.raises(m.GraphError):
        _run(go())


def test_sanction_reaches_the_owner_through_the_alias():
    async def go():
        g = m.InMemoryGraph()
        await m.ingest("doc", g, tenant="t1")
        cypher, params = m.CypherPlanner().plan("t1|Company:acme", ["OWNS", "SUPPLIES", "SANCTIONED_BY"], 3, 50, "t1")
        return await g.read(cypher, params)

    rows = _run(go())
    ends = {r["end"] for r in rows}
    assert "t1|Sanction:ofac 2024 17" in ends  # Acme -OWNS-> Borealis -SANCTIONED_BY-> OFAC (was missing)


def test_planned_cypher_returns_evidence():
    cypher, _ = m.CypherPlanner().plan("t1|Company:acme", ["OWNS"], 2, 5, "t1")
    assert "AS evidence" in cypher
    assert m.guard_cypher(cypher) == cypher
