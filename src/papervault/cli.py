from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import __version__, auth
from .core import State, VaultError, atomic_write, encode, read_json, uid
from .drive import BASE
from .index import compact
from .metadata import normalize_doi, title_of
from .service import Vault, summary


def parser():
    p = argparse.ArgumentParser(
        prog="pv", description="PaperVault：严格匹配 DOI、校验归档到 Google Drive，按需下载。"
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--home", help="独立本地状态目录（默认 ~/.papervault）")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth", help="Google OAuth 登录与本地凭据管理")
    s = a.add_subparsers(dest="action", required=True)
    login = s.add_parser("login")
    login.add_argument("--client")
    login.add_argument("--token-store", choices=["keyring", "file"], default="keyring")
    login.add_argument("--timeout", type=int, default=600)
    login.add_argument("--no-browser", action="store_true")
    s.add_parser("status")
    s.add_parser("logout")
    a = sub.add_parser("init", help="创建专用云端文献库")
    a.add_argument("--name", default="PaperVault")
    sub.add_parser("vaults", help="列出当前 OAuth 应用可见的文献库")
    a = sub.add_parser("connect", help="通过 vault.json 的 fileId 连接现有库")
    a.add_argument("config_id")
    a = sub.add_parser("config", help="查看或设置非敏感配置")
    a.add_argument("--mailto")
    a.add_argument("--cache-limit-mb", type=int)
    a.add_argument("--max-pdf-mb", type=int)
    sub.add_parser("doctor", help="诊断授权、Crossref 和 Drive 状态")
    a = sub.add_parser("import", help="导入 PDF；默认保留本地原件")
    a.add_argument("path")
    a.add_argument("--recursive", action="store_true")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--doi")
    policy = a.add_mutually_exclusive_group()
    policy.add_argument(
        "--force", action="store_true", help="重新识别并上传，核验后替换主文件、旧文件移入回收站"
    )
    policy.add_argument("--keep-version", action="store_true", help="同 DOI 的不同文件作为另一个版本保留")
    a.add_argument("--jsonl", action="store_true", help="逐文件输出结果，适用于大批量目录")
    a.add_argument(
        "--role",
        choices=["unspecified", "publisher", "accepted-manuscript", "annotated", "supplement"],
        default="unspecified",
    )
    a = sub.add_parser("review", help="人工复核队列")
    s = a.add_subparsers(dest="action", required=True)
    s.add_parser("list")
    a = s.add_parser("show")
    a.add_argument("job_id")
    a = s.add_parser("resolve")
    a.add_argument("job_id")
    a.add_argument("--doi", required=True)
    a.add_argument("--reason", required=True)
    a = sub.add_parser("jobs", help="导入状态和失败恢复")
    s = a.add_subparsers(dest="action", required=True)
    s.add_parser("list")
    a = s.add_parser("retry")
    a.add_argument("job_id", nargs="?")
    a.add_argument("--failed", action="store_true")
    a = sub.add_parser("search", help="搜索本库，或显式查询 Crossref")
    a.add_argument("query")
    a.add_argument("--remote", action="store_true")
    a.add_argument("--refresh", action="store_true")
    for name in ("show", "files"):
        a = sub.add_parser(name)
        a.add_argument("query")
    a = sub.add_parser("resolve", help="把 DOI、标题或参考文献文本解析为 Crossref 候选 DOI")
    a.add_argument("query")
    a = sub.add_parser("remove", help="移除云端 PDF，保留可恢复的文献历史；默认预览")
    a.add_argument("query")
    a.add_argument("--sha256")
    a.add_argument("--apply", action="store_true")
    a = sub.add_parser("replace", help="上传、核验后替换指定文献的文件；默认预览")
    a.add_argument("doi")
    a.add_argument("path")
    a.add_argument("--sha256", help="要替换的旧文件，默认主文件")
    a.add_argument("--apply", action="store_true")
    a = sub.add_parser("get", help="按 DOI 或唯一标题下载并校验")
    a.add_argument("query")
    a.add_argument("--output", default=".")
    a.add_argument("--sha256")
    a.add_argument("--resolve", action="store_true", help="先通过 Crossref 解析文本或 DOI")
    a.add_argument("--select-doi", help="文本有歧义时明确选择 Crossref 候选 DOI")
    a = sub.add_parser("open", help="下载到受管缓存并用系统阅读器打开")
    a.add_argument("query")
    a.add_argument("--sha256")
    a = sub.add_parser("primary", help="显式选择文献默认文件版本")
    a.add_argument("query")
    a.add_argument("--sha256", required=True)
    a = sub.add_parser("export", help="导出本地引文索引")
    a.add_argument("--format", choices=["csl-json"], default="csl-json")
    a.add_argument("--output", required=True)
    a = sub.add_parser("verify", help="完整下载校验云端文件，不保留验证副本")
    a.add_argument("--all", action="store_true", required=True)
    a = sub.add_parser("rebuild", help="只下载云端记录，重建本地索引")
    a.add_argument("--from-drive", action="store_true", required=True)
    a = sub.add_parser("metadata")
    s = a.add_subparsers(dest="action", required=True)
    a = s.add_parser("refresh")
    a.add_argument("query")
    a = sub.add_parser("release-local", help="核验后将本地原件移入回收站；默认只预览")
    a.add_argument("--batch", required=True)
    m = a.add_mutually_exclusive_group()
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--apply", action="store_true")
    a = sub.add_parser("cache")
    s = a.add_subparsers(dest="action", required=True)
    a = s.add_parser("prune")
    a.add_argument("--readers-closed", action="store_true", help="确认阅读器已关闭，解除缓存保护后按容量淘汰")
    sub.add_parser("recover", help="恢复元数据更新或 primary 修改的未完成提交")
    return p


def job_summary(j):
    m = j.get("match", {})
    return {
        k: v
        for k, v in {
            "job_id": j.get("job_id"),
            "batch_id": j.get("batch_id"),
            "source": j.get("source"),
            "status": j["status"],
            "doi": j.get("doi") or m.get("metadata", {}).get("doi"),
            "duplicate": j.get("duplicate", False),
            "skipped_processing": j.get("skipped_processing", False),
            "reason": j.get("reason"),
            "error": j.get("error") or m.get("reason"),
            "error_code": j.get("error_code"),
        }.items()
        if v is not None
    }


def result_code(jobs):
    codes = [
        j.get("error_code", 2)
        for j in jobs
        if j.get("status") not in ("archived", "local_released", "accepted", "skipped_existing_doi")
    ]
    return max(codes, default=0)


def dispatch(a, state):
    if a.cmd == "auth":
        if a.action == "login":
            return auth.login(state, a.client, a.token_store, a.timeout, not a.no_browser), 0
        if a.action == "logout":
            return auth.logout(state), 0
        return {
            "authorized": bool(auth.load_token(state)),
            "token_store": state.config.get("token_store", "keyring"),
        }, 0
    if a.cmd == "config":
        for name in ("mailto", "cache_limit_mb", "max_pdf_mb"):
            value = getattr(a, name)
            if value is not None:
                if isinstance(value, int) and (value < 0 or (name == "max_pdf_mb" and value == 0)):
                    raise VaultError("容量配置必须为正数，缓存上限允许为 0。", 4)
                state.config[name] = value
        state.save()
        return state.config, 0
    v = Vault(state)
    if a.cmd == "init":
        return v.init(a.name), 0
    if a.cmd == "vaults":
        return v.discover(), 0
    if a.cmd == "connect":
        return v.connect(a.config_id), 0
    if a.cmd == "doctor":
        out = {
            "home": str(state.home),
            "oauth_client_installed": (state.home / "client.json").exists(),
            "checks": [],
        }
        for name, check in [
            ("Crossref", lambda: bool(v.provider.get("/works", {"rows": 0}))),
            (
                "Google Drive",
                lambda: v.drive.request(
                    "GET", BASE + "/about", params={"fields": "user(displayName),storageQuota"}
                ).json(),
            ),
        ]:
            try:
                out["checks"].append({"name": name, "ok": True, "result": check()})
            except VaultError as e:
                out["checks"].append({"name": name, "ok": False, "error": str(e), "error_code": e.code})
        if state.config.get("vault"):
            try:
                vault = state.config["vault"]
                for k in ("root_id", "objects_id", "records_id"):
                    if v.drive.get(vault[k]).get("trashed"):
                        raise VaultError("文献库文件夹已移入回收站。")
                out["checks"].append({"name": "vault", "ok": True, "vault_id": vault["vault_id"]})
            except VaultError as e:
                out["checks"].append({"name": "vault", "ok": False, "error": str(e), "error_code": e.code})
        return out, max((x.get("error_code", 0) for x in out["checks"]), default=0)
    if a.cmd == "import":
        path = Path(a.path).expanduser()
        if path.is_dir():
            if a.doi:
                raise VaultError("--doi 仅可用于单个 PDF。", 2)
            paths = (
                p for p in (path.rglob("*") if a.recursive else path.iterdir()) if p.suffix.lower() == ".pdf"
            )
        else:
            paths = iter([path])
        if not a.dry_run:
            v.check_writer()
        batch = uid()
        counts = Counter()
        preview = []
        code = 0
        count = 0
        for count, path in enumerate(paths, 1):
            print(f"[{count}] {path.name}", file=sys.stderr)
            try:
                job = v.import_file(
                    path, batch, a.doi, a.dry_run, a.role, force=a.force, keep_version=a.keep_version
                )
            except (VaultError, OSError) as e:
                job = {
                    "status": "rejected",
                    "source": str(path),
                    "error": str(e),
                    "error_code": getattr(e, "code", 2),
                }
            counts[job["status"]] += 1
            item = job_summary(job)
            code = max(code, result_code([job]))
            if a.jsonl:
                print(json.dumps(item, ensure_ascii=False), flush=True)
            elif len(preview) < 100:
                preview.append(item)
        if not count:
            raise VaultError("没有找到 PDF。", 2)
        return {
            "batch_id": batch,
            "dry_run": a.dry_run,
            "counts": dict(counts),
            "total": count,
            "jobs": preview,
            "details_truncated": count > len(preview) and not a.jsonl,
        }, code
    if a.cmd == "resolve":
        return v.resolve(a.query), 0
    if a.cmd == "remove":
        return v.remove(a.query, a.sha256, a.apply), 0
    if a.cmd == "replace":
        doi = normalize_doi(a.doi)
        old = v.sync_work(doi)
        if not old or old.get("deleted"):
            raise VaultError("要替换的文献不存在。", 2)
        target = v.choose_file(old, a.sha256)
        if not a.apply:
            return {
                "doi": doi,
                "old_file": target,
                "new_path": str(Path(a.path).expanduser()),
                "action": "replace",
                "apply": False,
            }, 0
        j = v.import_file(a.path, uid(), doi, force=True, replace_sha=target["sha256"])
        return job_summary(j), result_code([j])
    if a.cmd == "review":
        if a.action == "list":
            return [
                job_summary(j)
                for j in v.index.jobs()
                if j["status"] in ("needs_review", "rejected", "blocked")
            ], 0
        j = v.index.job(a.job_id)
        if a.action == "show":
            return j, 0
        if not a.reason.strip():
            raise VaultError("人工核对理由不能为空。", 2)
        v.check_writer()
        j = v.run_job(j, manual_doi=a.doi, reason=a.reason)
        return job_summary(j), result_code([j])
    if a.cmd == "jobs":
        if a.action == "list":
            return [job_summary(j) for j in v.index.jobs()], 0
        if not a.job_id and not a.failed:
            raise VaultError("请指定 job_id 或 --failed。", 2)
        v.check_writer()
        jobs = (
            [v.index.job(a.job_id)]
            if a.job_id
            else [
                j
                for j in v.index.jobs()
                if j["status"]
                in ("retryable_error", "uploading", "blob_verified", "accepted", "discovered", "blocked")
            ]
        )
        jobs = [v.run_job(j) for j in jobs]
        return [job_summary(j) for j in jobs], result_code(jobs)
    if a.cmd == "search":
        if a.remote:
            items = []
            for r in v.provider.search(a.query):
                try:
                    doi = normalize_doi(r["DOI"])
                except (KeyError, VaultError):
                    continue
                items.append(
                    {
                        "doi": doi,
                        "title": title_of(r),
                        "archived_in_local_index": bool(v.index.find(doi)),
                        "source": "Crossref",
                    }
                )
            return items, 0
        return [summary(r) for r in v.find(a.query, a.refresh)], 0
    if a.cmd in ("show", "files"):
        r = v.one(a.query)
        return r if a.cmd == "show" else r["files"], 0
    if a.cmd == "get":
        if a.select_doi and not a.resolve:
            raise VaultError("--select-doi 需要 --resolve。", 2)
        query = v.resolve_doi(a.query, a.select_doi) if a.resolve else a.query
        return v.get(query, a.output, a.sha256), 0
    if a.cmd == "open":
        return v.open(a.query, a.sha256), 0
    if a.cmd == "export":
        items = [{**r["metadata"]["csl"], "id": r["work_id"]} for r in v.index.all()]
        out = Path(a.output).expanduser().resolve()
        if out.exists() or out.is_symlink():
            raise VaultError("导出路径已存在，拒绝覆盖。", 2)
        atomic_write(out, encode(items))
        return {"path": str(out), "items": len(items)}, 0
    if a.cmd == "rebuild":
        return v.sync(), 0
    if a.cmd == "verify":
        results = v.verify_all()
        return results, max((x.get("error_code", 0) for x in results), default=0)
    if a.cmd == "release-local":
        result = v.release(a.batch, a.apply)
        return result, 2 if any(x["action"] == "skipped" for x in result) else 0
    if a.cmd == "cache":
        return v.prune_cache(a.readers_closed), 0
    if a.cmd == "primary":
        return v.revise(a.query, primary=a.sha256), 0
    if a.cmd == "metadata":
        return v.revise(a.query, refresh=True), 0
    if a.cmd == "recover":
        v.check_writer()
        result = v.recover_operations()
        for p in sorted((state.home / "pending-records").glob("*.json")):
            r = read_json(p)
            v.sync_work(r["doi"])
            old = v.index.find(r["doi"])
            if old and v.index.commit_record(r["commit_id"]) == compact(r):
                p.unlink()
            elif old and old[0]["commit_id"] == r.get("parent_commit_id"):
                v.commit(r)
                p.unlink()
            else:
                raise VaultError("待恢复提交的父版本不匹配，停止恢复。")
            result.append({"commit_id": r["commit_id"], "recovered": True})
        return result, 0
    raise VaultError("未知命令。", 2)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Accept --json after the subcommand as well as before it.
    if "--json" in argv:
        argv.remove("--json")
        argv.insert(0, "--json")
    args = parser().parse_args(argv)
    try:
        state = State(args.home)
        with state.lock():
            state.save()
            result, code = dispatch(args, state)
        print(json.dumps(result, ensure_ascii=False, indent=None if getattr(args, "jsonl", False) else 2))
        return code
    except VaultError as e:
        if args.json:
            print(json.dumps({"error": str(e), "exit_code": e.code}, ensure_ascii=False))
        else:
            print(f"错误：{e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("已中断；导入进度已保留。", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 -- CLI boundary must redact credential-bearing exceptions.
        # Do not echo request objects, credential data or bearer session URLs.
        print(
            json.dumps(
                {
                    "error": "操作失败，请检查配置、网络或本地文件权限。",
                    "type": type(e).__name__,
                    "exit_code": 4,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 4


if __name__ == "__main__":
    sys.exit(main())
