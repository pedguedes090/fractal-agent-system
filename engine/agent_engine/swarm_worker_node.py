    def openhands_worker(state: PipelineState) -> dict[str, Any]:
        """HIERARCHICAL SWARM MODE — 1 Root + ≤20 Lead + ≤200 Specialist (max depth 2).
        SwarmOrchestrator manages full agent tree, A2A comms, file-ownership leases.
        """
        import concurrent.futures, queue, json as _json, re
        from .hierarchical_orchestrator import SwarmOrchestrator, MAX_LEADS, MAX_SPECIALISTS_PER_LEAD

        spec = (state.get("workerContext") or {}).get("workerTaskSpec") or {}
        direct_workspace = bool((state.get("executionEnvironment") or {}).get("directWorkspace"))
        setup_results = list(state.get("setupCommandResults") or [])
        setup_completed = bool(state.get("setupCommandsCompleted"))
        source_workspace = state.get("sourceWorkspacePath") or state.get("workspacePath", "")
        execution_id = str(state.get("executionId", ""))
        run_id = state.get("brokerRunId")
        overrides = state["settings"].get("modelOverrides") or {}
        coder_model = str(overrides.get("coder") or state["settings"]["model"])
        api_key_val = runtime_settings().get("apiKey", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        rework_ctx = state.get("latestReview")
        worker_attempt_base = (state.get("reworkCycle", 0) * 100) + state.get("retryCount", 0) + 1
        original_goal = (state.get("executionContext") or {}).get("originalUserGoal") or state.get("task", "")
        max_concurrency = min(int(os.environ.get("AGENT_CODER_PARALLELISM", "20")), 20)

        from .claude_code_worker import run_claude_code_worker, _has_sdk as _ccw_has_sdk
        if not _ccw_has_sdk():
            emit("openhands_worker", "claude-agent-sdk not installed")
            return {"workerAttempts": [{"error": "SDK missing", "changedFiles": []}],
                    "retryCount": state.get("retryCount", 0) + 1, "brokerEvents": state.get("brokerEvents", []),
                    "setupCommandResults": setup_results, "setupCommandsCompleted": setup_completed}

        # ── Init swarm ──
        swarm = SwarmOrchestrator(execution_id=execution_id, workspace_path=source_workspace,
                                   original_user_goal=original_goal, max_concurrency=max_concurrency)
        root = swarm.spawn_root(model=coder_model)
        emit("openhands_worker", f"🐝 ROOT={root.agent_id} · cap={MAX_LEADS}L×{MAX_SPECIALISTS_PER_LEAD}S")

        # ── Analyze workspace → spawn Lead agents ──
        from .hierarchical_orchestrator import LEAD_DOMAINS
        _ws_files: set[str] = set()
        try:
            from .workspace import walk_workspace
            _ws_files = {f["path"] for f in walk_workspace(source_workspace, max_files=300, max_depth=5)}
        except Exception: pass
        _domain_indicators = {
            "frontend": {".tsx", ".jsx", ".vue", ".svelte", ".html", ".css"},
            "backend": {".py", ".go", ".rs", ".java"},
            "database": {".sql", ".prisma", "migrations/"},
            "api": {"api/", "routes/", "controllers/"},
            "authentication": {"auth", "login", "oauth", "jwt"},
            "testing": {"test/", "tests/", "spec/", "__tests__/", ".test."},
            "build_tooling": {"Dockerfile", "Makefile", ".github/workflows"},
            "security": {".env", "vault", "cert", "ssl"},
        }
        _selected_domains = []
        for domain in LEAD_DOMAINS:
            if domain == "product_requirement": _selected_domains.append(domain); continue
            if any(ind in p for p in _ws_files for ind in _domain_indicators.get(domain, set())): _selected_domains.append(domain)
        if len(_selected_domains) < 3: _selected_domains = ["product_requirement","frontend","backend","testing","build_tooling","architecture"]
        _selected_domains = _selected_domains[:MAX_LEADS]
        leads = []
        for d in _selected_domains:
            a = swarm.spawn_lead(root.agent_id, d, model=coder_model)
            if a: leads.append(a); emit("openhands_worker", f"  Lead: {a.agent_id} [{d}]")
        emit("openhands_worker", f"🧭 {len(leads)} Leads across {len(_selected_domains)} domains")

        # ── Shared worktree ──
        wt_info = prepare_execution_worktree(source_workspace, execution_id)
        shared_workspace = str(wt_info.get("workspacePath") or source_workspace)
        if direct_workspace and not setup_completed:
            setup_results = run_setup_commands(state["workspacePath"], list(spec.get("commandsToRun") or []),
                target_project_dir=str(spec.get("targetProjectDir") or spec.get("projectRoot") or "."))
            setup_completed = True

        # ── Build task queue from Leads ──
        _all_tasks: queue.Queue[dict[str, Any]] = queue.Queue()
        _rework_files: list[str] = []
        if rework_ctx:
            for b in (rework_ctx.get("blockers") or []):
                _rework_files.extend(re.findall(r'([^\s:,]+\.(?:py|ts|tsx|js|jsx|vue|css|html|json|yaml|yml))[:(\d]', str(b)))
            for cr in (rework_ctx.get("commandResults") or []):
                for field in ("stdout", "stderr"):
                    _rework_files.extend(re.findall(r'([^\s:,]+\.(?:py|ts|tsx|js|jsx|vue|css|html))[:(\d]', str(cr.get(field, ""))))
            _rework_files = sorted(set(p.replace("\\", "/") for p in _rework_files))[:40]
        ti = 0
        for la in leads:
            d = la.role
            if _rework_files:
                df = [f for f in _rework_files if any(f.endswith(ext) for ext in {".tsx",".jsx",".vue",".html",".css"} if d=="frontend")]
                for f in df[:MAX_SPECIALISTS_PER_LEAD]:
                    sp = swarm.spawn_specialist(la.agent_id, f"Fix {f}", model=coder_model)
                    if sp: _all_tasks.put({"idx":ti,"label":sp.agent_id,"agent_id":sp.agent_id,"domain":d,"parent_id":la.agent_id,"prompt_extra":f"Fix error in {f}. Read diagnostics, apply minimal fix.","allowedFiles":[f],"forbiddenPaths":[]}); ti+=1
                continue
            if d == "product_requirement":
                sp = swarm.spawn_specialist(la.agent_id, "Write product spec", model=coder_model)
                if sp: _all_tasks.put({"idx":ti,"label":sp.agent_id,"agent_id":sp.agent_id,"domain":d,"parent_id":la.agent_id,"prompt_extra":"Analyze user goal+codebase. Write SPEC.md with acceptance criteria.","allowedFiles":["SPEC.md"],"forbiddenPaths":[]}); ti+=1
            elif d in ("frontend","backend","api","database"):
                for j in range(min(3, MAX_SPECIALISTS_PER_LEAD)):
                    sp = swarm.spawn_specialist(la.agent_id, f"Implement {d} slice {j+1}", model=coder_model)
                    if sp: _all_tasks.put({"idx":ti,"label":sp.agent_id,"agent_id":sp.agent_id,"domain":d,"parent_id":la.agent_id,"prompt_extra":f"Implement {d} functionality slice {j+1}/3.","allowedFiles":list(spec.get("allowedFiles") or []),"forbiddenPaths":list(spec.get("forbiddenPaths") or [])}); ti+=1
        total_items = _all_tasks.qsize()
        emit("openhands_worker", f"🐝 {len(leads)}L · {total_items} tasks · concurrency={max_concurrency}")

        # ── Work-stealing execution pool ──
        worker_results: list[dict[str, Any]] = []
        _res_lock = threading.Lock()

        def _run_sp(item):
            ai = item.get("agent_id","?")
            d = item.get("domain","?")
            if ai in swarm.agents: swarm.agents[ai].status = "running"; swarm.heartbeat(ai)
            sc = dict(spec); sc["allowedFiles"]=item.get("allowedFiles")or[]; sc["forbiddenPaths"]=item.get("forbiddenPaths")or[]
            if item.get("prompt_extra"): sc["subtaskBrief"]=item["prompt_extra"]
            for f in sc["allowedFiles"]: swarm.claim_file(ai, f)
            emit("openhands_worker",f"🐝 [{ai}] start [{d}]",node="openhands_worker",agent_role="coder",status="running")
            wr={}
            try:
                swarm.acquire()
                try: wr=run_claude_code_worker(workspace=shared_workspace,model=coder_model,api_key=api_key_val,worker_task_spec={**sc,"setupCommandResults":setup_results if item["idx"]==0 else [],"contextEnvelope":_context(state,"openhands_worker")},rework_context=rework_ctx,emit=(lambda s,d,_kw=None,**kw: emit(s,f"[{ai}] {d}",**kw) if _kw is None else emit(s,d,**{**kw,**(_kw or {})})),execution_id=f"{execution_id}-{ai}",worker_attempt=worker_attempt_base+item["idx"])
                finally: swarm.release()
                wr["agentId"]=ai; wr["domain"]=d
                ch=[f.get("path")or""for f in(wr.get("changedFiles")or[])]
                if ch: swarm.send_message(ai,"root","TASK_COMPLETED",task_id=ai,payload={"changedFiles":ch})
                if ai in swarm.agents: swarm.agents[ai].status="completed"
                emit("openhands_worker",f"✓ [{ai}] done · {len(ch)} files",node="openhands_worker",agent_role="coder",status="completed")
            except Exception as e:
                wr={"error":str(e),"agentId":ai,"domain":d,"changedFiles":[]}
                if ai in swarm.agents: swarm.agents[ai].status="failed"
                swarm.send_message(ai,"root","AGENT_CRASHED",task_id=ai,payload={"error":str(e)})
                emit("openhands_worker",f"✗ [{ai}] crash",node="openhands_worker",agent_role="coder",status="error",error=str(e)[:500])
            finally:
                for f in sc["allowedFiles"]: swarm.release_file(ai,f)
            with _res_lock: worker_results.append(wr)
            return wr

        if total_items<=1:
            while True:
                try: _run_sp(_all_tasks.get_nowait())
                except queue.Empty: break
        else:
            nt=min(max_concurrency,total_items)
            with concurrent.futures.ThreadPoolExecutor(max_workers=nt)as pool:
                fs=[]
                while True:
                    try: fs.append(pool.submit(_run_sp,_all_tasks.get_nowait()))
                    except queue.Empty: break
                for f in concurrent.futures.as_completed(fs):
                    try: f.result()
                    except Exception: pass

        # ── Merge + aggregate ──
        broker_events=state.get("brokerEvents",[])
        if wt_info.get("ready"):
            merge=merge_execution_worktree(wt_info,allowed_patterns=list(spec.get("allowedFiles")or[]),forbidden_patterns=list(spec.get("forbiddenPaths")or[]))
            cleanup_execution_worktree(wt_info)
        else: merge={"applied":[],"conflicts":[]}
        ac=list(merge.get("applied")or[]); es=[]
        for w in worker_results:
            if w.get("error"): es.append(f"[{w.get('agentId','?')}] {w['error']}")
            for f in(w.get("changedFiles")or[]):
                p=f.get("path")or""
                if p and p not in{c.get("path")for c in ac}: ac.append({"path":p,"status":"modified"})
        ok=len([w for w in worker_results if not w.get("error")])
        verdict=swarm.completion_verdict()
        tree_json=_json.dumps(swarm.tree_dict(),ensure_ascii=False)[:40000]
        emit("openhands_worker",f"🐝 verdict:{verdict.get('pass')} ok:{ok}/{len(worker_results)} stats:{swarm.stats}",node="openhands_worker",agent_role="orchestrator",status="completed",output=tree_json)
        combined={"summary":f"Swarm: {len(leads)}L,{total_items}T,{ok}ok,{len(ac)}files","changedFiles":ac,"policyViolations":merge.get("policyViolations")or[],"sandboxed":False,"setupCommandResults":setup_results,"swarmTree":swarm.tree_dict(),"swarmStats":swarm.stats,"completionVerdict":verdict}
        if es: combined["error"]="; ".join(es[:8])
        if direct_workspace and str(spec.get("projectStack")or"").lower()=="node":
            target=str(spec.get("targetProjectDir")or spec.get("projectRoot")or".")
            pr=Path(state["workspacePath"])if target in{"","."}else Path(state["workspacePath"])/target
            if(pr/"package.json").is_file():
                lk=next((p for p in("pnpm-lock.yaml","yarn.lock","bun.lock","bun.lockb")if(pr/p).is_file()),None)
                cmd={"pnpm-lock.yaml":"pnpm install","yarn.lock":"yarn install","bun.lock":"bun install","bun.lockb":"bun install"}.get(lk or"","npm install")
                emit("setup_commands",f"Deps: {cmd}"); setup_results.extend(run_setup_commands(state["workspacePath"],[cmd],target_project_dir=target))
        combined["setupCommandResults"]=setup_results
        sid=None
        if run_id:
            with _open_broker()as broker:
                st=broker.start_role(run_id,"coder",f"Swarm:{len(leads)}L,{total_items}T",_context(state,"openhands_worker"))
                sid=st["id"]; broker.complete_subtask(run_id,sid,"coder",combined,"failed"if es else"completed")
                broker_events=broker.events(run_id)
        if swarm.root_id and swarm.root_id in swarm.agents: swarm.agents[swarm.root_id].status="completed"
        return {"workerAttempts":[combined],"retryCount":state.get("retryCount",0)+1,"brokerEvents":broker_events,"setupCommandResults":setup_results,"setupCommandsCompleted":setup_completed}
