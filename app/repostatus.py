"""Repo freshness: is what we last built still what's on origin?

For each onboarded project this answers, cheaply and without touching the
working tree the build uses:

  * branch   — origin/<branch> tip (sha, date, subject)
  * parent   — origin/<merge_from> tip, and how many of its commits are NOT yet
               in origin/<branch> ("in sync" = parent tip is in branch history)
  * built    — how many commits on branch (+ parent, when auto-syncing) are not
               in the last successful build. A commit that only differs by a
               merge commit (e.g. you merged main into ios by hand on the mini
               and pushed, producing a different sha with identical content)
               counts as up to date: we compare with --no-merges and fall back
               to a tree comparison.
  * local    — the clone on the mini: commits on the local <branch> that were
               never pushed. The next build does `reset --hard origin/<branch>`,
               so those would be discarded — worth a warning after manual syncs.

Remote tips come from `git ls-remote` (no local writes, safe while a build is
running in the same repo). Only if a tip's objects aren't present locally do we
fetch, into a private ref namespace (refs/ubs-status/*) so we never move the
origin/* refs the build script relies on, and never while that project builds.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Optional

STATUS_NS = "refs/ubs-status"


def _env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


async def _git(cwd: str, *args: str, timeout: float = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
        env=_env(), start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode, out.decode(errors="replace").strip()


async def _has_commit(cwd: str, sha: str) -> bool:
    rc, _ = await _git(cwd, "cat-file", "-e", f"{sha}^{{commit}}", timeout=10)
    return rc == 0


async def _resolve(cwd: str, rev: str) -> str:
    rc, out = await _git(cwd, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", timeout=10)
    return out if rc == 0 else ""


async def _count(cwd: str, *revs: str, no_merges: bool = False) -> Optional[int]:
    args = ["rev-list", "--count"] + (["--no-merges"] if no_merges else []) + list(revs)
    rc, out = await _git(cwd, *args, timeout=30)
    try:
        return int(out) if rc == 0 else None
    except ValueError:
        return None


async def _commit_info(cwd: str, sha: str) -> dict:
    rc, out = await _git(cwd, "log", "-1", "--format=%H%x00%ct%x00%an%x00%s", sha, timeout=10)
    if rc != 0 or "\x00" not in out:
        return {"sha": sha, "short": sha[:8]}
    full, ct, author, subject = (out.split("\x00") + ["", "", ""])[:4]
    return {"sha": full, "short": full[:8], "time": int(ct or 0),
            "author": author, "subject": subject[:140]}


async def _pending_commits(cwd: str, include: list[str], exclude: list[str], n: int = 5) -> list[dict]:
    """Newest non-merge commits reachable from include but none of exclude."""
    rc, out = await _git(cwd, "log", "--no-merges", f"-n{n}",
                         "--format=%h%x00%ct%x00%an%x00%s", *include,
                         *[f"^{x}" for x in exclude], timeout=20)
    if rc != 0:
        return []
    items = []
    for line in out.splitlines():
        parts = line.split("\x00")
        if len(parts) == 4:
            items.append({"short": parts[0], "time": int(parts[1] or 0),
                          "author": parts[2], "subject": parts[3][:140]})
    return items


async def compute(local_path: str, branch: str, merge_from: str = "",
                  built_sha: str = "", attempted_sha: str = "",
                  allow_fetch: bool = True) -> dict:
    """Freshness of one project. Never raises; problems land in "error"/"notes"."""
    res: dict = {"checked_at": time.time(), "error": "", "notes": [],
                 "branch": None, "parent": None, "built": None, "attempted": None,
                 "local": None, "source": "remote"}
    cwd = local_path
    if not cwd or not (Path(cwd) / ".git").exists():
        res["error"] = "no git checkout at " + (cwd or "(unset)")
        return res
    parent = merge_from if merge_from and merge_from != branch else ""
    names = [branch] + ([parent] if parent else [])

    # 1. Remote tips straight from origin (what a build would see right now).
    tips: dict[str, str] = {}
    rc, out = await _git(cwd, "ls-remote", "--heads", "origin", *names, timeout=25)
    if rc == 0:
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                tips[ref[len("refs/heads/"):]] = sha.strip()
        for nm in names:
            if nm not in tips:
                res["notes"].append(f"branch '{nm}' does not exist on origin")
    else:
        # Offline / auth trouble: fall back to the last fetched origin/* refs so
        # the panel still says something useful, and say so.
        res["source"] = "last-fetch"
        res["notes"].append("couldn't reach origin (" + (out.splitlines()[-1] if out else f"rc={rc}")
                            + ") — using the last fetched refs")
        for nm in names:
            s = await _resolve(cwd, f"refs/remotes/origin/{nm}")
            if s:
                tips[nm] = s

    # 2. Make sure we have those commits locally; fetch into our own namespace.
    missing = [nm for nm in names if tips.get(nm) and not await _has_commit(cwd, tips[nm])]
    if missing:
        if allow_fetch:
            specs = [f"+refs/heads/{nm}:{STATUS_NS}/{nm}" for nm in missing]
            # --refmap= (empty) stops git's "opportunistic" update of
            # refs/remotes/origin/*, which it otherwise does even for explicit
            # refspecs — those refs belong to the build script.
            frc, fout = await _git(cwd, "fetch", "--no-tags", "--quiet", "--refmap=",
                                   "origin", *specs, timeout=300)
            if frc != 0:
                res["notes"].append("fetch failed: " + (fout.splitlines()[-1] if fout else f"rc={frc}"))
        else:
            res["notes"].append("a build is running in this checkout; new commits will be "
                                "compared after it finishes")
    have = {nm: s for nm, s in tips.items() if s and await _has_commit(cwd, s)}

    b_sha = have.get(branch, "")
    p_sha = have.get(parent, "") if parent else ""
    if b_sha:
        res["branch"] = {"name": branch, **await _commit_info(cwd, b_sha)}
    elif tips.get(branch):
        res["branch"] = {"name": branch, "sha": tips[branch], "short": tips[branch][:8],
                         "unfetched": True}

    # 3. Parent sync: commits on origin/<merge_from> not yet in origin/<branch>.
    if parent:
        info: dict = {"name": parent}
        if p_sha:
            info.update(await _commit_info(cwd, p_sha))
        elif tips.get(parent):
            info.update({"sha": tips[parent], "short": tips[parent][:8], "unfetched": True})
        if b_sha and p_sha:
            behind = await _count(cwd, f"{b_sha}..{p_sha}")
            behind_nm = await _count(cwd, f"{b_sha}..{p_sha}", no_merges=True)
            ahead = await _count(cwd, f"{p_sha}..{b_sha}", no_merges=True)
            info.update({"behind": behind, "behind_no_merges": behind_nm,
                         "branch_only": ahead, "in_sync": behind == 0})
            if behind:
                info["pending"] = await _pending_commits(cwd, [p_sha], [b_sha])
        res["parent"] = info

    # 4. Built vs. what a build would produce now (branch, plus parent if syncing).
    targets = [s for s in (b_sha, p_sha) if s]

    async def vs(sha_in: str) -> Optional[dict]:
        if not sha_in:
            return None
        sha = await _resolve(cwd, sha_in)
        if not sha:
            return {"sha": sha_in, "short": sha_in[:8], "unknown": True}
        out: dict = {"sha": sha, "short": sha[:8]}
        if not targets:
            return out
        behind_nm = await _count(cwd, *targets, f"^{sha}", no_merges=True)
        behind = await _count(cwd, *targets, f"^{sha}")
        same_tree = False
        if b_sha:
            _, t1 = await _git(cwd, "rev-parse", f"{sha}^{{tree}}", timeout=10)
            _, t2 = await _git(cwd, "rev-parse", f"{b_sha}^{{tree}}", timeout=10)
            same_tree = bool(t1) and t1 == t2 and (not p_sha or bool(res["parent"] and res["parent"].get("in_sync")))
        out.update({"behind": behind, "behind_no_merges": behind_nm, "same_tree": same_tree,
                    "current": same_tree or behind_nm == 0})
        if not out["current"]:
            out["pending"] = await _pending_commits(cwd, targets, [sha])
        return out

    res["built"] = await vs(built_sha)
    if attempted_sha and attempted_sha != built_sha:
        res["attempted"] = await vs(attempted_sha)

    # 5. The clone on the mini: unpushed commits on the local branch would be
    # thrown away by the next build's reset --hard.
    local: dict = {}
    rc, head_ref = await _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=10)
    local["checked_out"] = head_ref if rc == 0 else "(detached)"
    head = await _resolve(cwd, "HEAD")
    if head:
        local["head"] = head[:8]
    lb = await _resolve(cwd, f"refs/heads/{branch}")
    if lb and b_sha:
        # Commits that exist ONLY in the local branch: not on origin/<branch> and
        # (when auto-syncing) not on origin/<parent> either — parent commits get
        # re-merged by the next build, so they aren't at risk.
        excl = [b_sha] + ([p_sha] if p_sha else [])
        local["unpushed"] = await _count(cwd, lb, *[f"^{x}" for x in excl], no_merges=True) or 0
        local["unpushed_all"] = await _count(cwd, f"{b_sha}..{lb}") or 0
        if local["unpushed"]:
            local["unpushed_commits"] = await _pending_commits(cwd, [lb], excl)
    res["local"] = local
    res["checked_at"] = time.time()
    return res
