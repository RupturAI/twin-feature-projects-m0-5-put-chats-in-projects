#!/usr/bin/env python3
"""Apply this export folder to a checkout of the repo.

    python3 apply.py /path/to/repo [--dry-run] [--yes]

Files that haven't changed there since the branch started are copied as they
are. Files that have changed get a 3-way merge (git merge-file), so only the
branch's edits land; clashing lines get <<<<<<< markers. It shows the plan and
asks before writing, and backs up every file it changes. Needs git on PATH.

Exit code: 0 done, 1 conflicts or files to check, 2 error (nothing written).
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = b"<<<<<<< "
WRITES = ("create", "replace", "merge", "conflict")
TITLES = {
    "create": "create",
    "replace": "replace",
    "merge": "merge",
    "conflict": "CONFLICT",
    "delete": "delete",
    "mode": "mode",
    "attention": "CHECK",
}


class Stop(Exception):
    """Stops the run before anything is written (exit code 2)."""


class Action(object):
    def __init__(self, entry, kind, note="", data=None):
        self.entry = entry
        self.kind = kind
        self.note = note
        self.data = data

    @property
    def path(self):
        return self.entry["path"]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    with open(path, "rb") as f:
        return f.read()


def copy(src, dst):
    parent = os.path.dirname(dst)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    shutil.copy2(src, dst)


def run(cmd, cwd=None):
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        raise Stop("can't run %s: is it installed?" % cmd[0])
    out, err = p.communicate()
    return p.returncode, out, err.decode("utf-8", "replace").strip()


def in_export(kind, rel):
    return os.path.join(HERE, kind, *rel.split("/"))


def in_target(target, rel):
    path = os.path.join(target, *rel.split("/"))
    root = os.path.realpath(target)
    parent = os.path.realpath(os.path.dirname(path))
    if parent != root and not parent.startswith(root + os.sep):
        raise Stop("%s would land outside %s" % (rel, target))
    return path


def wants_exec(entry):
    return entry["new_mode"] == "100755"


def mode_changes(entry):
    return entry["old_mode"] != entry["new_mode"]


def is_exec(path):
    return bool(os.stat(path).st_mode & 0o111)


def set_exec(path, on):
    mode = os.stat(path).st_mode
    os.chmod(path, mode | ((mode & 0o444) >> 2) if on else mode & ~0o111)


# --- checks ---------------------------------------------------------------


def check_path(rel):
    parts = rel.split("/")
    if rel.startswith("/") or any(p in ("", ".", "..") for p in parts) or parts[0] == ".git":
        raise Stop("unsafe path in manifest.json: %r" % rel)


def verify(path, expected):
    try:
        data = read(path)
    except OSError:
        raise Stop("%s is missing from the export folder: copy the whole folder" % os.path.relpath(path, HERE))
    if sha256(data) != expected:
        raise Stop("%s is not what was exported (damaged in transfer?)" % os.path.relpath(path, HERE))


def load_export():
    try:
        with open(os.path.join(HERE, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise Stop("can't read manifest.json next to apply.py: %s" % e)
    if manifest.get("format") != 1:
        raise Stop("unknown export format: %r" % manifest.get("format"))
    for entry in manifest["files"]:
        check_path(entry["path"])
        if entry["status"] in ("A", "M"):
            verify(in_export("files", entry["path"]), entry["new_sha256"])
        if entry["status"] == "M":
            verify(in_export("base", entry["path"]), entry["base_sha256"])
    return manifest


def check_target(target, manifest):
    """Return the checkout's git dir, or stop if target is not the right checkout's top folder."""
    if not os.path.isdir(target):
        raise Stop("not a folder: %s" % target)
    rc, out, _ = run(["git", "-C", target, "rev-parse", "--show-toplevel"])
    if rc:
        raise Stop("not a git checkout: %s" % target)
    top = out.decode().strip()
    if os.path.realpath(top) != os.path.realpath(target):
        raise Stop("give the top folder of the checkout: %s" % top)
    touched = [e["path"] for e in manifest["files"] if e["status"] in ("M", "D")]
    if touched and not any(os.path.lexists(os.path.join(target, *p.split("/"))) for p in touched):
        raise Stop("none of the files this export changes exist in %s: wrong folder?" % target)
    rc, out, _ = run(["git", "-C", target, "rev-parse", "--git-dir"])
    return os.path.join(target, out.decode().strip())


# --- what earlier applies left behind -------------------------------------


class State(object):
    """What earlier applies of this branch brought here, kept under .git."""

    def __init__(self, gitdir, slug, target):
        self.dir = os.path.join(gitdir, "export-feature", slug)
        self.path = os.path.join(self.dir, "state.json")
        self.files = {}
        self.when = None
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.files = data.get("files", {})
            self.when = data.get("applied")
        # The earlier apply still counts while a file it wrote is untouched.
        # After a full revert none is, and its leftovers must not steer merges.
        self.active = False
        for rel, record in self.files.items():
            path = os.path.join(target, *rel.split("/"))
            if record.get("written_sha256") and os.path.isfile(path):
                if sha256(read(path)) == record["written_sha256"]:
                    self.active = True
                    break

    def record(self, rel):
        return self.files.get(rel) if self.active else None

    def applied(self, rel):
        return os.path.join(self.dir, "applied", *rel.split("/"))


class Context(object):
    def __init__(self, target, state, branch):
        self.target = target
        self.state = state
        self.branch = branch

    def merge(self, current, base, other, ours=False):
        cmd = ["git", "merge-file", "-p"] + (["--ours"] if ours else [])
        cmd += ["-L", "company", "-L", "base", "-L", self.branch, current, base, other]
        rc, out, _ = run(cmd, cwd=self.target)
        return rc, out


def previous_version(entry, ctx, current, base):
    """The branch version an earlier apply brought here, if the file still carries its edits.

    Merging against it brings only what changed on the branch since then. It is
    used only when the branch's starting version is the same as back then.
    """
    record = ctx.state.record(entry["path"])
    applied = ctx.state.applied(entry["path"])
    if not record or record.get("base_sha256") != entry["base_sha256"] or not os.path.isfile(applied):
        return None
    rc, merged = ctx.merge(current, base, applied, ours=True)
    return applied if rc == 0 and merged == read(current) else None


# --- planning ---------------------------------------------------------------


def same_or_mode(entry, dst):
    if mode_changes(entry) and is_exec(dst) != wants_exec(entry):
        return Action(entry, "mode", "becomes executable" if wants_exec(entry) else "stops being executable")
    return Action(entry, "same")


def plan_delete(entry, ctx, dst):
    if not os.path.isfile(dst):
        return Action(entry, "same")
    current = read(dst)
    applied = ctx.state.applied(entry["path"])
    ours = ctx.state.record(entry["path"]) and os.path.isfile(applied) and current == read(applied)
    if sha256(current) == entry["base_sha256"] or ours:
        return Action(entry, "delete")
    return Action(entry, "attention", "the branch deletes it, but it changed here; left alone")


def plan(entry, ctx):
    rel, status = entry["path"], entry["status"]
    dst = in_target(ctx.target, rel)
    if os.path.islink(dst) or (os.path.lexists(dst) and not os.path.isfile(dst)):
        return Action(entry, "attention", "a symlink or folder here; left alone")
    if status == "D":
        return plan_delete(entry, ctx, dst)
    new_path = in_export("files", rel)
    if not os.path.isfile(dst):
        if status == "A":
            return Action(entry, "create", data=read(new_path))
        return Action(entry, "attention", "the branch changes it, but it is deleted here; left alone")
    current, new = read(dst), read(new_path)
    if current == new:
        return same_or_mode(entry, dst)
    if status == "M" and sha256(current) == entry["base_sha256"]:
        return Action(entry, "replace", data=new)
    if current.startswith(MARKER) or b"\n" + MARKER in current:
        return Action(entry, "attention", "unresolved <<<<<<< markers: resolve them, then run again")
    base = in_export("base", rel) if status == "M" else os.devnull
    base = previous_version(entry, ctx, dst, base) or base
    rc, merged = ctx.merge(dst, base, new_path)
    if rc == 0:
        if merged == current:
            return same_or_mode(entry, dst)
        return Action(entry, "merge", "changed here too; merged", merged)
    if 0 < rc < 128:
        return Action(entry, "conflict", "%d conflict(s)" % rc, merged)
    return Action(entry, "attention", "can't merge it (binary?): kept this version; the branch's is in files/")


# --- writing ----------------------------------------------------------------


def new_dir(path):
    candidate, n = path, 2
    while os.path.exists(candidate):
        candidate, n = "%s-%d" % (path, n), n + 1
    os.makedirs(candidate)
    return candidate


def execute(actions, ctx):
    """Back up, then write/delete. Return the backup folder, if one was needed."""
    backups = None
    for a in actions:
        dst = in_target(ctx.target, a.path)
        if a.kind in ("replace", "merge", "conflict", "delete"):
            if backups is None:
                backups = new_dir(os.path.join(ctx.state.dir, "backups", time.strftime("%Y%m%d-%H%M%S")))
            copy(dst, os.path.join(backups, *a.path.split("/")))
        if a.kind in WRITES:
            parent = os.path.dirname(dst)
            if not os.path.isdir(parent):
                os.makedirs(parent)
            with open(dst, "wb") as f:
                f.write(a.data)
        elif a.kind == "delete":
            os.remove(dst)
        if a.kind in ("create", "mode") or (a.kind in WRITES and mode_changes(a.entry)):
            set_exec(dst, wants_exec(a.entry))
    return backups


def save_state(ctx, manifest, actions):
    state = ctx.state
    paths = set(e["path"] for e in manifest["files"])
    files = dict((rel, rec) for rel, rec in state.files.items() if rel in paths) if state.active else {}
    for a in actions:
        rel, entry = a.path, a.entry
        if entry["status"] == "D":
            if a.kind in ("delete", "same"):
                files.pop(rel, None)
            continue
        if a.kind == "attention":
            continue
        record = {"base_sha256": entry["base_sha256"], "new_sha256": entry["new_sha256"]}
        previous = state.record(rel) or {}
        if a.kind in WRITES:
            record["written_sha256"] = sha256(a.data)
        elif previous.get("written_sha256") == sha256(read(in_target(ctx.target, rel))):
            record["written_sha256"] = previous["written_sha256"]
        files[rel] = record
        copy(in_export("files", rel), state.applied(rel))
    if not os.path.isdir(state.dir):
        os.makedirs(state.dir)
    with open(state.path, "w", encoding="utf-8") as f:
        json.dump({
            "branch": manifest["branch"],
            "head": manifest["head"],
            "applied": time.strftime("%Y-%m-%d %H:%M:%S"),
            "files": files,
        }, f, indent=2)


# --- output -----------------------------------------------------------------


def git_line(target, *args):
    rc, out, _ = run(["git", "-C", target] + list(args))
    return out.decode().strip() if rc == 0 else ""


def dirty_paths(target):
    paths = set()
    for args in (["diff", "--name-only", "-z", "HEAD"], ["ls-files", "-z", "-o", "--exclude-standard"]):
        rc, out, _ = run(["git", "-C", target] + args)
        if rc == 0:
            paths.update(p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p)
    return paths


def print_plan(m, ctx, actions, dropped):
    print("%s @ %s: %d commit(s) on top of %s @ %s"
          % (m["branch"], m["head"][:7], len(m["commits"]), m["base_ref"], m["base"][:7]))
    branch = git_line(ctx.target, "symbolic-ref", "--short", "-q", "HEAD") or "detached"
    print("Into: %s (%s @ %s)" % (ctx.target, branch, git_line(ctx.target, "rev-parse", "--short", "HEAD")))
    if ctx.state.active and ctx.state.when:
        print("Builds on the apply of this branch from %s." % ctx.state.when)
    if os.path.realpath(HERE).startswith(os.path.realpath(ctx.target) + os.sep):
        print("Note: this export folder is inside the checkout; move it out or delete it before committing.")
    print("")
    dirty = dirty_paths(ctx.target)
    for a in actions:
        if a.kind == "same":
            continue
        extra = "  (%s)" % a.note if a.note else ""
        if a.path in dirty:
            extra += "  [had uncommitted changes]"
        print("  %-8s %s%s" % (TITLES[a.kind], a.path, extra))
    for rel in dropped:
        print("  %-8s %s  (an earlier export of this branch changed it, this one doesn't)" % ("CHECK", rel))
    same = sum(a.kind == "same" for a in actions)
    if same:
        print("  %d file(s) already up to date" % same)


def print_summary(m, ctx, actions, dropped, backups):
    words = [("create", "created"), ("replace", "replaced"), ("merge", "merged"),
             ("conflict", "with conflicts"), ("delete", "deleted"), ("mode", "mode changes")]
    done = ["%d %s" % (n, word) for kind, word in words for n in [sum(a.kind == kind for a in actions)] if n]
    print("\nDone: %s." % (", ".join(done) or "nothing to write"))
    conflicts = [a.path for a in actions if a.kind == "conflict"]
    if conflicts:
        print("Resolve the <<<<<<< markers in:")
        for path in conflicts:
            print("  " + path)
    checks = [(a.path, a.note) for a in actions if a.kind == "attention"]
    checks += [(rel, "changed by an earlier export of this branch, not by this one") for rel in dropped]
    if checks:
        print("Check these (left alone):")
        for path, note in checks:
            print("  %s: %s" % (path, note))
    if backups:
        print("Backups of the files it changed: %s" % backups)
    print("Commits in this export:")
    for c in m["commits"]:
        print("  %s %s" % (c["sha"][:7], c["subject"]))
    print("Next: cd %s && git status && git diff" % ctx.target)


def confirm(target):
    try:
        answer = input("\nApply to %s? [y/N] " % target)
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def parse_args(argv):
    p = argparse.ArgumentParser(description="Apply this export to a checkout of the repo.")
    p.add_argument("target", help="top folder of the checkout, e.g. ~/twin")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p.add_argument("--yes", action="store_true", help="don't ask before writing")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    target = os.path.abspath(os.path.expanduser(args.target))
    try:
        manifest = load_export()
        gitdir = check_target(target, manifest)
        ctx = Context(target, State(gitdir, manifest["slug"], target), manifest["branch"])
        actions = [plan(entry, ctx) for entry in manifest["files"]]
    except Stop as e:
        print("apply: %s" % e, file=sys.stderr)
        return 2
    paths = set(e["path"] for e in manifest["files"])
    dropped = sorted(rel for rel in ctx.state.files if rel not in paths) if ctx.state.active else []
    print_plan(manifest, ctx, actions, dropped)
    problems = dropped or [a for a in actions if a.kind in ("conflict", "attention")]
    todo = [a for a in actions if a.kind not in ("same", "attention")]
    if args.dry_run:
        print("\nDry run: nothing written.")
        return 1 if problems else 0
    if todo and not args.yes and not confirm(target):
        print("Nothing written.")
        return 1
    try:
        backups = execute(todo, ctx)
        save_state(ctx, manifest, actions)
    except (OSError, Stop) as e:
        print("apply: stopped while writing: %s" % e, file=sys.stderr)
        print("Backups so far are under %s" % os.path.join(ctx.state.dir, "backups"), file=sys.stderr)
        return 2
    print_summary(manifest, ctx, actions, dropped, backups)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
