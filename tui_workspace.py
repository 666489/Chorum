"""Visible terminal workspace and keyboard-driven model/effort menus."""
import json
import time

VERSION = "0.4.0"


def sidebar(app, width, ui):
    lines = [("成员与模型", ui.C_INPUT), ("F2 切模型   F3 调 effort", ui.C_SYS), ("", "")]
    for idx, name in enumerate(app.agent_order):
        a = app.agents[name]
        st = app.status.get(name) or {}
        selected = not app.targets or name in app.targets
        running = st.get("state") == "running"
        code = ui.C_OK if running else ui.C_INPUT if selected else ui.C_SYS
        lines += [(str(idx+1) + "  " + ui._md_clean(a.get("label", name)) + ("  执行中" if running else ""), code),
                  ("   " + ui._md_clean(a.get("model") or "CLI 默认模型"), ui.C_SYS),
                  ("   effort: " + (a.get("effort") or "auto") + (" · 已停用" if not a.get("enabled", True) else ""), ui.C_SYS), ("", "")]
    lines += [("协作模式", ui.C_INPUT), ("F1  圆桌讨论 / 持续执行", ui.C_SYS),
              ("F6  主代理只调度一次", ui.C_SYS), ("F7  长期 / 短期记忆", ui.C_SYS),
              ("", ""), ("原始目标始终优先", ui.C_SYS), ("独立主张 · 依赖 · 检查点", ui.C_SYS)]
    return [(ui.clip_disp(t, width), c) for t,c in lines]


def menu_lines(menu, width, ui):
    lines = [("", ""), ("  " + menu["title"], ui.C_INPUT), ("  " + menu.get("hint", "↑/↓ 选择，Enter 应用，Esc 返回"), ui.C_SYS), ("", "")]
    if menu.get("input"):
        value = menu.get("text", "")
        lines.extend(("  " + part, ui.C_USER) for part in ui.wrap_text(value or "输入内容…", max(10,width-6)))
        lines += [("", ""), ("  Enter 确认 · Esc 取消；不会修改讨论输入草稿", ui.C_SYS)]
    else:
        for idx, item in enumerate(menu.get("items", [])):
            on = idx == menu.get("index",0)
            lines.append((ui.clip_disp(("  ▸ " if on else "    ") + item["label"], width), ui.C_INPUT if on else ui.C_SYS))
            if item.get("note"):
                lines.append((ui.clip_disp("      " + item["note"],width),ui.C_SYS))
    if menu.get("error"):
        lines += [("", "")] + [("  " + p,ui.C_ERR) for p in ui.wrap_text(menu["error"],max(10,width-6))]
    return lines


def render(app, cols, rows, ui):
    width = max(40,cols)
    wide = width >= 96 and rows >= 22
    side_width = 25 if wide else 0
    body_width = width - side_width - (5 if wide else 2)
    body_height = max(5, rows-8)
    menu = getattr(app,"menu",None)
    if menu:
        body = menu_lines(menu,body_width,ui)
    elif app.rt_wizard is not None and app.rt_wizard.get("stage") == "setup":
        body = app.rt_setup_lines(body_width)
    elif app.view == "memory":
        body=[("  项目记忆",ui.C_INPUT),("  F1 → 保存短期/长期记忆；/memory 关键词检索",ui.C_SYS),("","")]
        for m in getattr(app,"memory_entries",[]):
            body.append(("  ["+m["kind"]+"] "+m.get("source",""),ui.C_HL))
            body.extend(ui.render_markdown_lines(m["text"],body_width,ui.C_SYS))
        if len(body)==3: body.append(("  尚无记忆。任务检查点会保存为短期记忆。",ui.C_SYS))
    else:
        body=app.flat_lines(body_width)
        if app.view=="messages":
            live=ui.build_stream_lines(app.status,app.agents,body_width,app.show_think)
            if live: body=body+[("","")]+live
    if not body:
        body=[("", ""),("  从原始目标开始",ui.C_INPUT),("", ""),
              ("  F1 开始多智能体讨论，或 F6 让主代理调度一次。",ui.C_SYS),
              ("  执行代理持续工作，直到完成、阻塞或达到预算。",ui.C_SYS),("", ""),
              ("  F2 自由切换模型    F3 设置 effort",ui.C_SYS),
              ("  F4 查看工具调用    F5 滚动查看推理摘要",ui.C_SYS)]
    if app.scroll>0 and app._last_msgs and len(body)>app._last_msgs:
        app.scroll+=len(body)-app._last_msgs
    app._last_msgs=len(body)
    app.scroll=min(app.scroll,max(0,len(body)-body_height))
    if menu and not menu.get("input"):
        selected=4+sum(1+bool(item.get("note")) for item in menu.get("items",[])[:menu.get("index",0)])
        if selected >= app.scroll+body_height: app.scroll=selected-body_height+1
        if selected < app.scroll: app.scroll=selected
    top = bool(menu) or bool(app.rt_wizard) or len(body)<body_height
    start = app.scroll if top else max(0,len(body)-body_height-app.scroll)
    visible=body[start:start+body_height]
    visible+= [("","")]*(body_height-len(visible))
    online="已连接" if app.connected else "连接中"
    version=getattr(app,"server_version","") or "版本待确认"
    head=" ClaudeLink "+VERSION+"  /  协作工作台"
    right=online+" · 会话 #"+str(app.conv_id or "—")+" "
    lines=[(ui.C_PANEL+ui.pad_cells(ui.clip_disp(head,width-ui.disp_width(right)),width-ui.disp_width(right))+right+ui.RESET,""),
           (ui.clip_disp(" 服务 "+version+"  ·  "+app.client.host+":"+str(app.client.port)+"  ·  "+str(len(app.agent_order))+" 位成员",width),ui.C_SYS)]
    tabs=[]
    for key,label in [("messages","讨论"),("tools","工具记录"),("thinking","推理摘要"),("tasks","持续执行"),("memory","记忆")]:
        tabs.append((ui.C_PANEL if app.view==key else ui.C_SYS)+" "+label+" "+ui.RESET)
    lines.append((ui.clip_disp(" "+"  ".join(tabs),width),""))
    lines.append(("─"*width,ui.C_SYS))
    side=sidebar(app,side_width,ui) if wide else []
    for idx,(text,code) in enumerate(visible):
        main=code+ui.clip_disp(text,body_width)+ui.RESET
        if wide:
            left,style=side[idx] if idx<len(side) else ("","")
            line=" "+style+ui.pad_cells(left,side_width)+ui.RESET+ui.C_SYS+" │ "+ui.RESET+main
        else: line=" "+main
        lines.append((line,""))
    lines.append(("─"*width,ui.C_SYS))
    target="全体" if not app.targets else ",".join(app.labels.get(n,n) for n in app.targets)
    prefix=" 发送给 "+target+" › "
    if app.rt_wizard: prefix=app.rt_wizard_prompt()
    raw=app.editor.text()
    before=raw[:app.editor.cur].replace("\n","↵")
    available=max(4,width-ui.disp_width(prefix)-2)
    text=raw.replace("\n","↵")
    if ui.disp_width(before)>available:
        before="…"+before[-max(1,available//2):]
        text=before+text[app.editor.cur:]
    lines.append((ui.clip_disp(prefix+text,width),ui.C_INPUT))
    lines.append((ui.clip_disp(" F1 操作  F2 模型  F3 effort  F4 工具  F5 推理  F6 执行  F7 记忆  Ctrl+O 视图",width),ui.C_SYS))
    lines.append((ui.clip_disp(" Enter 发送 · Tab 切成员 · 滚轮/PgUp/PgDn 回看 · Ctrl+C 退出",width),ui.C_SYS))
    out=["\x1b[?25l"]
    for i in range(rows):
        text,style=lines[i] if i<len(lines) else ("","")
        out += [f"\x1b[{i+1};1H",style+ui.clip_disp(text,width)+ui.RESET,"\x1b[K"]
    cursor_row=min(rows,5+body_height+1)
    cursor_col=min(width,1+ui.disp_width(prefix)+ui.disp_width(before))
    out.append(f"\x1b[{cursor_row};{cursor_col}H\x1b[?25h")
    return "".join(out)


def open_menu(app, action):
    if action=="actions":
        items=[("开始圆桌讨论","round"),("主代理调度一次，执行代理持续工作","work"),("切换模型","model"),
               ("设置推理 effort","effort"),("保存短期记忆","remember_short"),("保存长期记忆","remember_long"),("暂停全部执行","stop")]
        app.menu={"title":"协作操作","action":"actions","items":[{"label":l,"value":v} for l,v in items],"index":0}
    elif action=="memory":
        app.memory_entries=app.client.get("/api/memory").get("memories",[])
        app.view="memory"
        app.menu=None
    elif action in ("remember_short","remember_long"):
        app.menu={"title":"保存"+("长期" if action.endswith("long") else "短期")+"记忆","action":action,"input":True,"text":""}
    else:
        items=[{"label":a.get("label",n),"value":n,"note":(a.get("model") or "CLI 默认")+" · effort "+(a.get("effort") or "auto")} for n in app.agent_order for a in [app.agents[n]] if a.get("enabled",True)]
        if not items: raise ValueError("没有启用的成员；先在 Web 成员面板添加或启用模型")
        app.menu={"title":{"model":"选择要切换模型的成员","effort":"选择要设置 effort 的成员","work":"选择主代理（只调度一次）"}[action],"action":"agent_"+action,"items":items,"index":0}
    app.scroll,app._last_msgs,app.dirty=0,0,True


def menu_key(app,key):
    menu=app.menu
    if key in ("paste_start","paste_end"):
        menu["pasting"]=key=="paste_start"
        return
    if key=="esc": app.menu=None;app.dirty=True;return
    if key=="ctrl_c": app._stopping=True;return
    if menu.get("input"):
        if isinstance(key,tuple) and key[0]=="char": menu["text"]+=key[1]
        elif key=="backspace": menu["text"]=menu["text"][:-1]
        elif key=="enter" and menu.get("pasting"):
            menu["text"]+="\n"
        elif key=="enter":
            value=menu["text"].strip()
            if not value: menu["error"]="内容不能为空"
            else:
                try:
                    action=menu["action"]
                    if action=="model_input":
                        result=app.client.post("/api/agents/model",{"agent":menu["agent"],"model":value})
                    elif action=="work_input":
                        workers=app.targets or [n for n in app.agent_order if app.agents[n].get("enabled")]
                        result=app.client.post("/api/workflows/start",{"planner":menu["agent"],"agents":workers,"topic":value})
                        app.view="tasks"
                    else:
                        result=app.client.post("/api/memory",{"kind":"long" if action.endswith("long") else "short","text":value,"source":"user"})
                    if result.get("ok") is False: raise ValueError(result.get("error") or "操作失败")
                    app.menu=None;app._reload_pending=True;app.notice("已保存")
                except Exception as exc: menu["error"]=str(exc)
    elif key in ("up","down"):
        menu["index"]=(menu.get("index",0)+(-1 if key=="up" else 1))%len(menu["items"])
    elif key=="enter":
        item=menu["items"][menu.get("index",0)]
        action=menu["action"]
        try:
            if action=="actions":
                app.menu=None
                if item["value"]=="round": app.rt_begin_wizard()
                elif item["value"]=="stop": app.command("/stop")
                else: open_menu(app,item["value"])
            elif action=="agent_work":
                app.menu={"title":"输入原始目标与验收条件","action":"work_input","agent":item["value"],"input":True,"text":""}
            elif action=="agent_model":
                agent=item["value"]
                choices=app.client.get("/api/model-choices?agent="+agent).get("choices",[]) if app.agents[agent].get("kind","cli")=="cli" else []
                defaults=[{"label":"跟随 CLI 默认","value":""}] if app.agents[agent].get("kind","cli")=="cli" else []
                app.menu={"title":"切换模型 · "+item["label"],"action":"model_value","agent":agent,"index":0,
                          "items":[{"label":"手动输入任意模型 ID","value":"__custom"}]+defaults+choices}
            elif action=="agent_effort":
                agent=item["value"]
                values=app.client.get("/api/capabilities").get("effort",{}).get(agent,[""])
                app.menu={"title":"设置 effort · "+item["label"],"action":"effort_value","agent":agent,"index":0,
                          "items":[{"label":v or "auto（跟随默认）","value":v} for v in values]}
            elif action=="model_value" and item["value"]=="__custom":
                app.menu={"title":"输入模型 ID","action":"model_input","agent":menu["agent"],"input":True,"text":""}
            else:
                kind="model" if action=="model_value" else "effort"
                payload={"agent":menu["agent"],kind:item["value"]}
                if item.get("kind")=="profile": payload={"agent":menu["agent"],"settings_profile":item["value"],"model":""}
                result=app.client.post("/api/agents/"+kind,payload)
                if result.get("ok") is False: raise ValueError(result.get("error") or "操作失败")
                app.menu=None;app._reload_pending=True;app.notice("设置已应用")
        except Exception as exc: menu["error"]=str(exc);app.menu=menu
    app.dirty=True
