import json
import threading

import pytest

from app.classification.models import Level
from app.graph import ActionGraph, Node, graph


@pytest.fixture
def g() -> ActionGraph:
    return ActionGraph()


class TestAddNode:
    def test_empty_graph_starts_with_no_root(self, g: ActionGraph):
        assert g.root is None
        assert g.nodes == []

    def test_first_node_becomes_root(self, g: ActionGraph):
        root = g.add_node("root", threshold=0.5)
        assert g.root is root
        assert root.neighbors == []
        assert root.threshold == 0.5
        assert root.tool is None

    def test_second_root_rejected(self, g: ActionGraph):
        g.add_node("root")
        with pytest.raises(ValueError, match="root already exists"):
            g.add_node("another-root")

    def test_first_node_cannot_connect_in_empty_graph(self, g: ActionGraph):
        with pytest.raises(ValueError, match="first node must be the root"):
            g.add_node("orphan", connect=Node(id="ghost"))

    def test_edge_links_nodes_mutually(self, g: ActionGraph):
        root = g.add_node("root")
        tool = object()
        child = g.add_node("child", connect=root, threshold=1.0, tool=tool)
        assert root.neighbors == [child]
        assert child.neighbors == [root]
        assert child.tool is tool

    def test_duplicate_id_rejected(self, g: ActionGraph):
        g.add_node("root")
        with pytest.raises(ValueError, match="already exists"):
            g.add_node("root")

    def test_unknown_connect_target_rejected(self, g: ActionGraph):
        g.add_node("root")
        with pytest.raises(ValueError, match="not in the graph"):
            g.add_node("child", connect=Node(id="ghost"))

    @pytest.mark.parametrize("threshold", [0.0, 1.0, 0.42])
    def test_threshold_bounds_inclusive(self, g: ActionGraph, threshold: float):
        node = g.add_node("n", threshold=threshold)
        assert node.threshold == threshold

    @pytest.mark.parametrize("threshold", [-0.1, 1.1, 100, "0.5", None, True])
    def test_threshold_out_of_range_or_wrong_type(self, g: ActionGraph, threshold):
        with pytest.raises((TypeError, ValueError), match="threshold"):
            g.add_node("n", threshold=threshold)

    def test_empty_id_rejected(self, g: ActionGraph):
        with pytest.raises(ValueError, match="non-empty string"):
            g.add_node("")

    def test_get_node(self, g: ActionGraph):
        node = g.add_node("root")
        assert g.get_node("root") is node
        assert g.get_node("missing") is None


class TestConnect:
    def test_connect_adds_a_mutual_edge(self, g: ActionGraph):
        root = g.add_node("root")
        left = g.add_node("left", connect=root)
        right = g.add_node("right", connect=root)
        g.connect(left, right)
        assert root.neighbors == [left, right]
        assert left.neighbors == [root, right]
        assert right.neighbors == [root, left]

    def test_diamond_node_lists_all_its_neighbors(self, g: ActionGraph):
        root = g.add_node("root")
        a = g.add_node("a", connect=root)
        b = g.add_node("b", connect=a)
        c = g.add_node("c", connect=a)
        g.connect(b, c)
        # c is connected to BOTH a and b: two neighbors, no parent concept
        assert c.neighbors == [a, b]
        assert a.neighbors == [root, b, c]

    def test_undirected_loop_traversal_terminates(self, g: ActionGraph):
        root = g.add_node("root")
        a = g.add_node("a", connect=root)
        b = g.add_node("b", connect=a)
        g.connect(b, root)  # loop root -- a -- b -- root

        assert [n.id for n in g.reachable(root)] == ["a", "b"]

    def test_connect_rejects_duplicate_edge(self, g: ActionGraph):
        root = g.add_node("root")
        child = g.add_node("child", connect=root)
        with pytest.raises(ValueError, match="already exists"):
            g.connect(root, child)

    def test_connect_rejects_self_loop(self, g: ActionGraph):
        root = g.add_node("root")
        with pytest.raises(ValueError, match="self-loop"):
            g.connect(root, root)

    def test_connect_rejects_nodes_outside_the_graph(self, g: ActionGraph):
        root = g.add_node("root")
        with pytest.raises(ValueError, match="not in the graph"):
            g.connect(Node(id="ghost"), root)
        with pytest.raises(ValueError, match="not in the graph"):
            g.connect(root, Node(id="ghost"))

    def test_connect_rejects_stale_instances(self, g: ActionGraph):
        stale = g.add_node("stale")
        g.clear()
        other = ActionGraph()
        root = other.add_node("root")
        with pytest.raises(ValueError, match="not in the graph"):
            other.connect(stale, root)


class TestPersistence:
    def test_save_load_roundtrip(self, g: ActionGraph, tmp_path):
        root = g.add_node("root", threshold=0.1)
        left = g.add_node("left", connect=root, threshold=0.5)
        g.add_node("right", connect=root, threshold=0.9)
        g.add_node("leaf", connect=left, threshold=0.0)

        path = tmp_path / "graph.json"
        g.save(path)
        payload = json.loads(path.read_text())
        assert payload["root"] == "root"
        assert payload["nodes"][0] == {
            "id": "root",
            "neighbors": ["left", "right"],
            "threshold": 0.1,
        }

        restored = ActionGraph()
        restored.load(path)
        assert [n.id for n in restored.nodes] == ["root", "left", "right", "leaf"]
        assert {n.id: n.threshold for n in restored.nodes} == {
            "root": 0.1,
            "left": 0.5,
            "right": 0.9,
            "leaf": 0.0,
        }
        assert restored.root is restored.get_node("root")
        assert [c.id for c in restored.get_node("root").neighbors] == ["left", "right"]
        assert restored.get_node("left").neighbors == [
            restored.get_node("root"),
            restored.get_node("leaf"),
        ]
        assert restored.get_node("leaf").neighbors == [restored.get_node("left")]

    def test_save_load_roundtrip_preserves_a_loop(self, g: ActionGraph, tmp_path):
        root = g.add_node("root")
        a = g.add_node("a", connect=root)
        b = g.add_node("b", connect=a)
        g.connect(b, root)  # loop root -- a -- b -- root

        path = tmp_path / "graph.json"
        g.save(path)
        restored = ActionGraph()
        restored.load(path)

        assert restored.root is restored.get_node("root")
        # Neighbor order after load follows snapshot wiring, not insertion: compare as sets.
        assert {n.id for n in restored.get_node("root").neighbors} == {"a", "b"}
        assert {n.id for n in restored.get_node("a").neighbors} == {"root", "b"}
        assert {n.id for n in restored.get_node("b").neighbors} == {"a", "root"}

    def test_save_empty_graph(self, g: ActionGraph, tmp_path):
        path = tmp_path / "empty.json"
        g.save(path)
        assert json.loads(path.read_text()) == {"root": None, "nodes": []}

        restored = ActionGraph()
        restored.load(path)
        assert restored.root is None
        assert restored.nodes == []

    def test_save_leaves_no_temp_files(self, g: ActionGraph, tmp_path):
        path = tmp_path / "graph.json"
        g.add_node("root")
        g.save(path)
        assert list(tmp_path.iterdir()) == [path]

    def test_tool_is_not_persisted(self, g: ActionGraph, tmp_path):
        g.add_node("root", tool=object())
        path = tmp_path / "graph.json"
        g.save(path)

        restored = ActionGraph()
        restored.load(path)
        assert restored.get_node("root").tool is None

    def test_load_replaces_existing_graph(self, g: ActionGraph, tmp_path):
        g.add_node("old-root")
        other = ActionGraph()
        other.add_node("new-root")

        path = tmp_path / "graph.json"
        other.save(path)
        g.load(path)

        assert [n.id for n in g.nodes] == ["new-root"]
        assert g.root.id == "new-root"

    def test_load_rejects_snapshot_with_unknown_neighbor(self, g: ActionGraph, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps(
                {"root": "a", "nodes": [{"id": "a", "neighbors": ["ghost"], "threshold": 0.0}]}
            )
        )
        with pytest.raises(ValueError, match="unknown neighbor"):
            g.load(path)

    def test_load_rejects_snapshot_with_self_loop(self, g: ActionGraph, tmp_path):
        path = tmp_path / "self-loop.json"
        path.write_text(
            json.dumps({"root": "a", "nodes": [{"id": "a", "neighbors": ["a"], "threshold": 0.0}]})
        )
        with pytest.raises(ValueError, match="self-loop"):
            g.load(path)

    def test_load_rejects_snapshot_with_duplicate_edges(self, g: ActionGraph, tmp_path):
        path = tmp_path / "dupe.json"
        path.write_text(
            json.dumps(
                {
                    "root": "a",
                    "nodes": [
                        {"id": "a", "neighbors": ["b", "b"], "threshold": 0.0},
                        {"id": "b", "neighbors": [], "threshold": 0.0},
                    ],
                }
            )
        )
        with pytest.raises(ValueError, match="duplicate edge"):
            g.load(path)

    def test_failed_load_preserves_current_graph(self, g: ActionGraph, tmp_path):
        g.add_node("keeper")
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps(
                {
                    "root": "a",
                    "nodes": [
                        {"id": "a", "neighbors": ["ghost"], "threshold": 0.0},
                        {"id": "b", "neighbors": [], "threshold": 0.0},
                    ],
                }
            )
        )
        with pytest.raises(ValueError):
            g.load(path)
        assert [n.id for n in g.nodes] == ["keeper"]
        assert g.root.id == "keeper"

    def test_load_rejects_snapshot_with_nodes_but_no_root(self, g: ActionGraph, tmp_path):
        path = tmp_path / "rootless.json"
        path.write_text(json.dumps({"nodes": [{"id": "a", "neighbors": [], "threshold": 0.0}]}))
        with pytest.raises(ValueError, match="no root"):
            g.load(path)

    def test_load_rejects_snapshot_with_root_not_among_nodes(self, g: ActionGraph, tmp_path):
        path = tmp_path / "ghost-root.json"
        path.write_text(
            json.dumps({"root": "ghost", "nodes": [{"id": "a", "neighbors": [], "threshold": 0.0}]})
        )
        with pytest.raises(ValueError, match="not among the nodes"):
            g.load(path)

    def test_load_accepts_snapshot_without_optional_fields(self, g: ActionGraph, tmp_path):
        path = tmp_path / "minimal.json"
        path.write_text(
            json.dumps(
                {"root": "root", "nodes": [{"id": "root", "neighbors": [], "threshold": 0.5}]}
            )
        )
        g.load(path)
        assert g.get_node("root").level == Level.NONE
        assert g.get_node("root").run_id is None


class TestThreadSafety:
    def test_concurrent_adds(self, g: ActionGraph):
        root = g.add_node("root")
        errors: list[Exception] = []

        def add(i: int) -> None:
            try:
                g.add_node(f"node-{i}", connect=root)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(g.nodes) == 101
        assert len(root.neighbors) == 100


class TestSingleton:
    def test_module_level_instance_is_shared(self):
        assert isinstance(graph, ActionGraph)
        assert ActionGraph() is not graph


class TestRunSubtrees:
    def test_ensure_run_creates_root_and_run_node(self, g: ActionGraph):
        run_node = g.ensure_run("r1")
        assert g.root.id == "root"
        assert run_node.id == "run:r1"
        assert g.root.neighbors == [run_node]
        assert run_node.neighbors == [g.root]
        assert run_node.run_id == "r1"
        assert run_node.level == Level.NONE

    def test_ensure_run_is_idempotent(self, g: ActionGraph):
        first = g.ensure_run("r1")
        second = g.ensure_run("r1")
        assert first is second
        assert len(g.nodes) == 2

    def test_runs_share_the_single_root(self, g: ActionGraph):
        a = g.ensure_run("a")
        b = g.ensure_run("b")
        assert g.root.neighbors == [a, b]

    def test_ensure_run_rejects_empty_id(self, g: ActionGraph):
        with pytest.raises(ValueError, match="non-empty"):
            g.ensure_run("")

    def test_append_chains_under_the_run_node(self, g: ActionGraph):
        run_node = g.ensure_run("r")
        n1 = g.append("r", level=Level.MILD, threshold=0.9, intent="recon")
        n2 = g.append("r", level=Level.SEVERE, threshold=0.8, intent="exfiltrate_secrets")
        assert [n1.id, n2.id] == ["r:1", "r:2"]
        assert run_node.neighbors == [g.root, n1]
        assert n1.neighbors == [run_node, n2]
        assert n2.neighbors == [n1]
        assert n1.run_id == "r" and n2.run_id == "r"

    def test_append_creates_the_run_lazily(self, g: ActionGraph):
        node = g.append("r", level=Level.MILD, threshold=0.9)
        assert g.root.id == "root"
        assert g.get_node("run:r").neighbors == [g.root, node]

    def test_append_validates_threshold(self, g: ActionGraph):
        with pytest.raises(ValueError, match="threshold"):
            g.append("r", level=Level.MILD, threshold=1.5)

    def test_append_rejects_non_level(self, g: ActionGraph):
        with pytest.raises(ValueError):
            g.append("r", level=99, threshold=0.9)

    def test_run_nodes_in_order_and_isolated_per_run(self, g: ActionGraph):
        g.ensure_run("a")
        g.ensure_run("b")
        a1 = g.append("a", level=Level.MILD, threshold=0.9)
        g.append("b", level=Level.MODERATE, threshold=0.9)
        a2 = g.append("a", level=Level.SEVERE, threshold=0.9)
        assert [n.id for n in g.run_nodes("a")] == ["run:a", "a:1", "a:2"]
        assert [n.id for n in g.run_nodes("b")] == ["run:b", "b:1"]
        assert g.run_nodes("a") == [g.get_node("run:a"), a1, a2]
        assert g.run_nodes("missing") == []

    def test_run_isolation_survives_extra_edges_between_runs(self, g: ActionGraph):
        # Undirected graph: runs are isolated by their run_id stamp, not by
        # edge direction, so even explicit cross-run edges cannot leak nodes.
        g.ensure_run("a")
        g.ensure_run("b")
        a1 = g.append("a", level=Level.MILD, threshold=0.9)
        b1 = g.append("b", level=Level.SEVERE, threshold=0.9)
        g.connect(a1, b1)
        assert [n.id for n in g.run_nodes("a")] == ["run:a", "a:1"]
        assert g.level("a") == Level.MILD
        assert g.level("b") == Level.SEVERE

    def test_key_nodes_exclude_sub_mild_levels(self, g: ActionGraph):
        g.ensure_run("r")
        n1 = g.append("r", level=Level.MILD, threshold=0.9)
        g.append("r", level=Level.NONE, threshold=0.9)
        n3 = g.append("r", level=Level.SEVERE, threshold=0.9)
        assert g.key_nodes("r") == [n1, n3]

    def test_level_is_max_over_the_run(self, g: ActionGraph):
        g.ensure_run("r")
        assert g.level("r") == Level.NONE
        g.append("r", level=Level.MILD, threshold=0.9)
        g.append("r", level=Level.SEVERE, threshold=0.9)
        g.append("r", level=Level.MODERATE, threshold=0.9)
        assert g.level("r") == Level.SEVERE
        assert g.level("missing") == Level.NONE

    def test_actionable_level_holds_then_releases(self, g: ActionGraph):
        g.ensure_run("r")
        g.append("r", level=Level.SEVERE, threshold=0.5)
        assert g.level("r") == Level.SEVERE
        assert g.actionable_level("r") == Level.NONE
        g.append("r", level=Level.SEVERE, threshold=0.9)
        assert g.actionable_level("r") == Level.SEVERE

    def test_actionable_level_gate_is_inclusive(self, g: ActionGraph):
        g.ensure_run("r")
        g.append("r", level=Level.MODERATE, threshold=0.7)
        assert g.actionable_level("r") == Level.MODERATE
        g.append("r", level=Level.SEVERE, threshold=0.69)
        assert g.actionable_level("r") == Level.MODERATE

    def test_clear_resets_the_instance_in_place(self, g: ActionGraph):
        g.ensure_run("r")
        g.append("r", level=Level.MILD, threshold=0.9)
        g.clear()
        assert g.root is None
        assert g.nodes == []
        assert g.get_node("run:r") is None


class TestUpdate:
    def test_update_sets_documented_fields(self, g: ActionGraph):
        node = g.append("r", level=Level.MILD, threshold=0.9)
        updated = g.update(node.id, level=Level.SEVERE, intent="lateral_movement", action_id="a1")
        assert updated.level == Level.SEVERE
        assert updated.intent == "lateral_movement"
        assert updated.action_id == "a1"

    def test_update_rejects_bad_threshold(self, g: ActionGraph):
        node = g.add_node("n")
        with pytest.raises(ValueError, match="threshold"):
            g.update(node.id, threshold=-1)

    def test_update_unknown_node_raises(self, g: ActionGraph):
        with pytest.raises(ValueError, match="does not exist"):
            g.update("ghost", level=Level.MILD)

    def test_update_unknown_field_raises(self, g: ActionGraph):
        node = g.add_node("n")
        with pytest.raises(ValueError, match="unknown node fields"):
            g.update(node.id, neighbors=None)


class TestRunPersistence:
    def test_save_load_roundtrip_preserves_run_fields(self, g: ActionGraph, tmp_path):
        g.ensure_run("r")
        n1 = g.append(
            "r", level=Level.MILD, threshold=0.9, intent="recon", event={"event": "file_read"}
        )
        n2 = g.append("r", level=Level.SEVERE, threshold=0.8, action_id="a1")

        path = tmp_path / "graph.json"
        g.save(path)
        restored = ActionGraph()
        restored.load(path)

        run_node = restored.get_node("run:r")
        r1 = restored.get_node(n1.id)
        r2 = restored.get_node(n2.id)
        assert r1 is not None and r2 is not None
        assert r1.run_id == "r" and r2.run_id == "r"
        assert r1.level == Level.MILD and r2.level == Level.SEVERE
        assert r1.intent == "recon"
        assert r1.event == {"event": "file_read"}
        assert r2.action_id == "a1"
        assert r1.created_at == n1.created_at
        assert run_node.neighbors == [restored.root, r1]
        assert r1.neighbors == [run_node, r2]
        assert r2.neighbors == [r1]
        assert restored.level("r") == Level.SEVERE
        assert [n.id for n in restored.key_nodes("r")] == [n1.id, n2.id]

    def test_save_load_roundtrip_preserves_created_at_on_update(self, g: ActionGraph, tmp_path):
        node = g.append("r", level=Level.MILD, threshold=0.9)
        g.update(node.id, level=Level.SEVERE, intent="recon")
        path = tmp_path / "graph.json"
        g.save(path)
        restored = ActionGraph()
        restored.load(path)
        updated = restored.get_node(node.id)
        assert updated.level == Level.SEVERE
        assert updated.intent == "recon"
        assert updated.created_at == node.created_at
