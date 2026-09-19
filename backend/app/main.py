from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.actions.router import router as actions_router
from app.evals.router import (
    lab_benchmarks_page,
    lab_conversation_page,
    lab_css,
    lab_inspector_page,
    lab_page,
)
from app.evals.router import (
    router as evals_router,
)
from app.graph import graph as action_graph
from app.graph.neo4j import neo4j_graph
from app.graph.router import router as graph_router
from app.realtime.router import router as realtime_router
from app.runs.router import router as runs_router
from app.world.router import router as world_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    await neo4j_graph.setup()
    if action_graph.revision == 0:
        for run_id in await neo4j_graph.all_run_ids():
            steps = await neo4j_graph.restore_classification(run_id)
            action_graph.hydrate_run(run_id, steps)
    yield
    await neo4j_graph.close()


app = FastAPI(title="hackspain", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(actions_router, prefix="/api/demo", tags=["demo actions"])
app.include_router(evals_router, prefix="/api/evals", tags=["evals"])
app.include_router(runs_router, prefix="/api/runs", tags=["runs"])
app.include_router(realtime_router, prefix="/api/runs", tags=["realtime"])
app.include_router(graph_router, prefix="/api/graph", tags=["graph"])
app.include_router(world_router, prefix="/api/world", tags=["world"])


@app.get("/health")
def health():
    return {"status": "ok"}


app.get("/lab", tags=["evals"])(lab_page)
app.get("/lab/conversation", tags=["evals"])(lab_conversation_page)
app.get("/lab/benchmarks", tags=["evals"])(lab_benchmarks_page)
app.get("/lab/inspector", tags=["evals"])(lab_inspector_page)
app.get("/lab.css", tags=["evals"])(lab_css)
