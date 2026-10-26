"""HTTP-level test for undo/redo on the shot editor.

`shot_edit_test.py` covers the pure functions; this script covers the wire: the
routes, the response flags the UI buttons read (`can_undo`/`can_redo`), the 409
when a stack is empty, and the fact that history survives a fresh client because
it lives in the sidecar rather than in memory.

Runs against an in-process ASGI app (``TestClient``) with a throwaway DATA_DIR, so
it needs no running server and never touches real projects. The only external
dependency is ffmpeg, used to synthesise a 12 s clip for the project to point at.

    backend/.venv/bin/python -m backend.scripts.editor_http_test
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Point every store at a temp tree *before* backend.config is imported: paths are
# resolved once at import time.
TMP = Path(tempfile.mkdtemp(prefix="clipforge-http-"))
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ["TEMP_DIR"] = str(TMP / "temp")
os.environ["EXPORTS_DIR"] = str(TMP / "exports")

from fastapi.testclient import TestClient  # noqa: E402

from backend import config  # noqa: E402
from backend.main import app  # noqa: E402
from backend.schemas.analyze import ViralClip  # noqa: E402
from backend.schemas.project import ProjectCreate, ProjectSource, ProjectVideo  # noqa: E402
from backend.services import project_store, shot_store, video_engine  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")
    return ok


def make_clip(path: Path, seconds: float = 12.0) -> None:
    ffmpeg = video_engine.capabilities().get("path") or "ffmpeg"
    subprocess.run(
        [
            ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )


def main() -> int:
    key = "http_test"
    make_clip(config.MEDIA_DIR / f"{key}.mp4")

    project = project_store.create(
        ProjectCreate(
            name="HTTP undo/redo test",
            source=ProjectSource(type="upload", video_id=key),
            video=ProjectVideo(title="synthetic", duration=12.0),
            language="id",
            clips=[
                ViralClip(
                    title="synthetic clip",
                    start_time=0.0,
                    end_time=12.0,
                    virality_score=50,
                )
            ],
        )
    )
    pid = project.id
    base = f"/api/editor/shots/{pid}"

    seed = [
        {"index": 1, "start": 0.0, "end": 4.0, "kind": "follow_speaker"},
        {"index": 2, "start": 4.0, "end": 8.0, "kind": "follow_speaker"},
        {"index": 3, "start": 8.0, "end": 12.0, "kind": "follow_speaker"},
    ]
    shot_store.save(pid, 0, seed, window=(0.0, 12.0))

    print("undo/redo over HTTP")
    with TestClient(app) as client:
        r = client.get(base, params={"clip_index": 0})
        body = r.json()
        check("GET stored list", r.status_code == 200 and body["origin"] == "stored", r.text[:160])
        check("GET 3 shots", len(body["shots"]) == 3, str(len(body["shots"])))
        check("fresh list has no history",
              body["can_undo"] is False and body["can_redo"] is False
              and body["undo_depth"] == 0 and body["redo_depth"] == 0,
              json.dumps({k: body[k] for k in ("can_undo", "can_redo", "undo_depth", "redo_depth")}))

        r = client.post(f"{base}/patch", json={"clip_index": 0, "index": 2, "kind": "whole_frame"})
        body = r.json()
        check("PATCH retargets shot", r.status_code == 200 and body["shots"][1]["kind"] == "whole_frame", r.text[:160])
        check("PATCH makes the edit undoable",
              body["can_undo"] is True and body["undo_depth"] == 1 and body["can_redo"] is False,
              json.dumps({k: body[k] for k in ("can_undo", "can_redo", "undo_depth")}))

        r = client.post(f"{base}/split", json={"clip_index": 0, "at": 6.0})
        body = r.json()
        check("SPLIT adds a shot", r.status_code == 200 and len(body["shots"]) == 4, r.text[:160])
        check("SPLIT deepens history", body["undo_depth"] == 2, str(body["undo_depth"]))

        r = client.post(f"{base}/undo", json={"clip_index": 0})
        body = r.json()
        check("UNDO returns 200", r.status_code == 200, r.text[:160])
        check("UNDO restores 3 shots", len(body["shots"]) == 3, str(len(body["shots"])))
        check("UNDO offers redo",
              body["can_redo"] is True and body["redo_depth"] == 1 and body["can_undo"] is True,
              json.dumps({k: body[k] for k in ("can_undo", "can_redo", "undo_depth", "redo_depth")}))

        r = client.post(f"{base}/undo", json={"clip_index": 0})
        body = r.json()
        check("UNDO to the seed list", r.status_code == 200 and body["shots"][1]["kind"] == "follow_speaker", r.text[:160])
        check("UNDO emptied the past stack",
              body["can_undo"] is False and body["undo_depth"] == 0 and body["redo_depth"] == 2,
              json.dumps({k: body[k] for k in ("can_undo", "undo_depth", "redo_depth")}))

        r = client.post(f"{base}/undo", json={"clip_index": 0})
        check("UNDO with an empty stack is 409", r.status_code == 409 and "Nothing to undo" in r.text, r.text[:160])

        r = client.post(f"{base}/redo", json={"clip_index": 0})
        body = r.json()
        check("REDO re-applies the patch",
              r.status_code == 200 and len(body["shots"]) == 3 and body["shots"][1]["kind"] == "whole_frame",
              r.text[:160])
        r = client.post(f"{base}/redo", json={"clip_index": 0})
        body = r.json()
        check("REDO re-applies the split", r.status_code == 200 and len(body["shots"]) == 4, r.text[:160])
        check("REDO at the tip clears redo",
              body["can_redo"] is False and body["redo_depth"] == 0, str(body["redo_depth"]))

        r = client.post(f"{base}/redo", json={"clip_index": 0})
        check("REDO with an empty stack is 409", r.status_code == 409 and "Nothing to redo" in r.text, r.text[:160])

    # A brand-new client must still see the same history: nothing lives in memory.
    with TestClient(app) as client:
        body = client.get(base, params={"clip_index": 0}).json()
        check("history survives a new client",
              body["can_undo"] is True and body["undo_depth"] == 2 and body["redo_depth"] == 0,
              json.dumps({k: body[k] for k in ("can_undo", "redo_depth", "undo_depth")}))

        client.post(f"{base}/undo", json={"clip_index": 0})
        body = client.post(f"{base}/patch", json={"clip_index": 0, "index": 1, "zoom": 1.2}).json()
        check("a new edit after undo drops the redo branch",
              body["can_redo"] is False and body["redo_depth"] == 0, str(body["redo_depth"]))
        check("the new edit is undoable", body["can_undo"] is True, str(body["can_undo"]))

    print("history cap")
    with TestClient(app) as client:
        for i in range(shot_store.HISTORY_MAX + 5):
            client.post(f"{base}/patch", json={"clip_index": 0, "index": 1, "zoom": round(1.0 + i * 0.001, 3)})
        body = client.get(base, params={"clip_index": 0}).json()
        check(f"history stops at {shot_store.HISTORY_MAX}",
              body["undo_depth"] == shot_store.HISTORY_MAX, str(body["undo_depth"]))
        for _ in range(shot_store.HISTORY_MAX):
            r = client.post(f"{base}/undo", json={"clip_index": 0})
            if r.status_code != 200:
                break
        check("undoing the whole cap works", r.status_code == 200, str(r.status_code))
        r = client.post(f"{base}/undo", json={"clip_index": 0})
        check("then the stack is empty again", r.status_code == 409, str(r.status_code))

    print("error paths")
    with TestClient(app) as client:
        r = client.post("/api/editor/shots/prj_20200101_deadbeef/undo", json={"clip_index": 0})
        check("undo on an unknown project is 404", r.status_code == 404, str(r.status_code))
        r = client.post(f"{base}/undo", json={"clip_index": 7})
        check("undo on a missing clip is 404", r.status_code == 404, str(r.status_code))
        r = client.post(f"{base}/undo", json={"clip_index": -1})
        check("negative clip index is rejected", r.status_code == 422, str(r.status_code))

        r = client.delete(base, params={"clip_index": 0})
        body = r.json()
        check("DELETE resets to the draft", r.status_code == 200 and body["origin"] == "draft", r.text[:160])
        check("DELETE clears the history",
              body["can_undo"] is False and body["undo_depth"] == 0 and body["redo_depth"] == 0,
              json.dumps({k: body[k] for k in ("can_undo", "undo_depth", "redo_depth")}))
        r = client.post(f"{base}/undo", json={"clip_index": 0})
        check("undo after reset is 409", r.status_code == 409, str(r.status_code))

    print("sidecar shape")
    with TestClient(app) as client:
        client.post(f"{base}/patch", json={"clip_index": 0, "index": 1, "zoom": 1.1})
        raw = json.loads((config.SHOTS_DIR / f"{pid}_0.json").read_text(encoding="utf-8"))
        check("sidecar keeps past/future", isinstance(raw.get("past"), list) and isinstance(raw.get("future"), list),
              str(sorted(raw.keys())))

    print(f"\n{PASSED} checks passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(code)
