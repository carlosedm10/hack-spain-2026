"""Runnable tour of the ActionGraph (undirected): build, inspect, save, load.

Run it locally from repo root:

    cd backend && PYTHONPATH=. uv run python examples/graph_demo.py

Or inside the container (PYTHONPATH=/app is already set there):

    docker compose exec backend-hackspain uv run python examples/graph_demo.py
"""

from pathlib import Path

from app.graph import ActionGraph

SNAPSHOT = Path("/tmp/graph_demo.json")


def show(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def dump(g: ActionGraph) -> None:
    for n in g.nodes:
        print(f"  {n.id:<6} -- {[x.id for x in n.neighbors]}")


# ---------------------------------------------------------------- build
g = ActionGraph()

show("1. BUILD  root -- a -- b   (every edge is mutual)")
root = g.add_node("root", threshold=0.1)
a = g.add_node("a", connect=root, threshold=0.9, level=3, intent="recon")
b = g.add_node("b", connect=a, threshold=0.8, level=4, intent="exfil")
dump(g)

show("2. ADD c CONNECTED TO BOTH a AND b  (c.neighbors == [a, b])")
c = g.add_node("c", connect=a, threshold=0.7, level=2, intent="lateral")
g.connect(b, c)
dump(g)

show("3. READS  (cycle-safe: nothing hangs)")
print("  nodes:          ", [n.id for n in g.nodes])
print("  c.neighbors:    ", [n.id for n in c.neighbors], " <- connected to a AND b")
print("  reachable(root):", [n.id for n in g.reachable(root)], " <- each node once")

# ------------------------------------------------------------- save/load
show("4. SAVE  (JSON on disk)")
g.save(SNAPSHOT)
print(SNAPSHOT.read_text())

show("5. LOAD into a fresh graph")
restored = ActionGraph()
restored.load(SNAPSHOT)
dump(restored)

ok = (
    [n.id for n in restored.get_node("c").neighbors] == ["a", "b"]
    and restored.get_node("a").neighbors
    == [restored.get_node("root"), restored.get_node("b"), restored.get_node("c")]
    and restored.root is restored.get_node("root")
)
print(f"\n  neighbors survive the roundtrip: {ok}")
