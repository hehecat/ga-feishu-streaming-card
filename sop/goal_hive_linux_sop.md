# Goal Hive Mode SOP（Linux）

## 定义

Goal Hive = Goal Mode 的多 worker 协作协议。

本文件是 Linux/POSIX 启动版；Hive 模式单独运行，不要和 plan、supervisor、subagent 混用。

> 说明：源码中的 `agent_team_worker.py` 仍是旧的“读取最近 10 条 `/posts`”实现。本 SOP 不修改源码；默认在本轮工作目录生成一个兼容 reflector，用 `/poll?since_id=...&limit=50` 顺序排空消息、持久化游标并跳过自身发帖，从而避免冷启动重放和窗口漏帖。若改回直接使用原 worker，必须把这些轮询残差列入风险，不得宣称已修复。

## 0. 前置检查

在 GenericAgent 根目录执行：

```bash
cd <CodeRoot>
uv --version
[ -x .venv/bin/python ] || { echo '缺少项目 .venv，请先按项目方式准备环境'; exit 1; }
```

禁止使用系统 `pip`、`python -m venv`、`--break-system-packages`。

新 Hive 必须使用未占用的工作目录；不要默默复用旧数据库。以下变量在**同一个 shell**中顺序执行，尖括号内容须先替换：

```bash
CODE_ROOT="$(pwd)"
PORT=<空闲端口>
BBS_CWD="$CODE_ROOT/temp/hive_<目标短名>"
BOARD_KEY_FILE="$BBS_CWD/.board_key"
DUTY_FILE="$CODE_ROOT/memory/goal_hive_master_duty.md"
OBJECTIVE_FILE="$BBS_CWD/objective.txt"
HIVE_NAME="<本轮 Hive 名称>"

# 不把 key 放进命令行、URL、公开日志或全局记忆；本 SOP 为新目录随机生成它。
umask 077
export CODE_ROOT PORT BBS_CWD BOARD_KEY_FILE DUTY_FILE OBJECTIVE_FILE HIVE_NAME
```

## 1. 创建工作目录

```bash
if [ -d "$BBS_CWD" ] && [ -n "$(find "$BBS_CWD" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  echo "工作目录非空，拒绝复用：$BBS_CWD；请换新目录或走显式恢复流程" >&2
  exit 1
fi
mkdir -p "$BBS_CWD/evidence"

cat > "$OBJECTIVE_FILE" <<'EOF'
<在这里填写用户原始目标、明确交付物和验收标准>
EOF
test -s "$OBJECTIVE_FILE"
test -s "$DUTY_FILE"
```

`objective.txt` 是本轮唯一的目标输入；不要把报告、猜测或“待验证”文字当成目标写入它。

## 2. 启动 BBS

不要使用 `--key`：该参数会把密钥暴露在 BBS 进程命令行中。让 `agent_bbs.py` 从本轮私有 `boards.json` 加载板块；其 `--cwd` 会使该文件和数据库位于 `BBS_CWD`。

```bash
# 仅新目录执行；key 文件和 boards.json 都是本轮私有文件。
( umask 077; uv run python -c 'import secrets; print(secrets.token_hex(32))' > "$BOARD_KEY_FILE" )
uv run python - "$BBS_CWD/boards.json" "$BOARD_KEY_FILE" <<'PY'
import json, pathlib, sys
boards_path, key_path = map(pathlib.Path, sys.argv[1:])
key = key_path.read_text(encoding='utf-8').strip()
if not key or any(ch.isspace() for ch in key):
    raise SystemExit('invalid generated board key')
boards_path.write_text(json.dumps({key: {'name': 'hive', 'db': 'hive_bbs.db'}},
                                  ensure_ascii=False, indent=2), encoding='utf-8')
PY
chmod 600 "$BOARD_KEY_FILE" "$BBS_CWD/boards.json"

BBS_URL="http://127.0.0.1:$PORT"
export BBS_URL
# curl 只接收私有 config 的路径，不把 key/token 放进 curl 的 argv。
cat > "$BBS_CWD/.curl-auth.conf" <<EOF
header = "X-API-Key: $(<"$BOARD_KEY_FILE")"
EOF
chmod 600 "$BBS_CWD/.curl-auth.conf"
bbs_curl() {
  curl --silent --show-error --fail --max-time 10 \
    --config "$BBS_CWD/.curl-auth.conf" "$@"
}

nohup uv run python "$CODE_ROOT/assets/agent_bbs.py" \
  --cwd "$BBS_CWD" \
  --port "$PORT" \
  > "$BBS_CWD/bbs.log" 2>&1 &
BBS_PID=$!
printf '%s\n' "$BBS_PID" > "$BBS_CWD/bbs.pid"

# 以实际 API 响应判定启动成功，并确认 daemon 命令行没有 --key。
for i in $(seq 1 20); do
  if bbs_curl "$BBS_URL/readme" > "$BBS_CWD/readme.txt"; then break; fi
  sleep 1
done
test -s "$BBS_CWD/readme.txt" || {
  echo 'BBS 启动失败'; tail -80 "$BBS_CWD/bbs.log"; exit 1;
}
BBS_CMDLINE=$(tr '\0' ' ' < "/proc/$BBS_PID/cmdline" 2>/dev/null || true)
case "$BBS_CMDLINE" in
  *--key*|*BOARD_KEY*) echo 'BBS 命令行疑似含密钥参数，拒绝继续' >&2; exit 1 ;;
esac
printf 'BBS ready: %s\n' "$BBS_URL"
```

不要把包含 key 的 URL 放入帖子或公共日志；`readme.txt`、`.board_key`、`.curl-auth.conf`、`boards.json`、数据库和 token JSON 均只留在本轮目录，并保持 `0600`。不要开启 `set -x`。

## 5. 注册、首帖与 Goal Master

BBS 客户端必须从私有文件读取 key；不要把 key 或 token 放入命令行、URL、帖子、`ps` 输出或公开日志。以下客户端使用 Python 标准库直接请求，不产生携密的子进程 argv：

```bash
cat > "$BBS_CWD/bbs_client.py" <<'PY'
import json, pathlib, sys
from urllib import request
root = pathlib.Path(__file__).parent
base = sys.argv[1]
key = (root/'.board_key').read_text(encoding='utf-8').strip()
def call(method, path, body=None):
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = request.Request(base+path, data=raw, method=method,
                          headers={'X-API-Key':key, 'Content-Type':'application/json'})
    with request.urlopen(req, timeout=10) as r:
        return json.load(r)
op=sys.argv[2]
if op == 'register':
    print(json.dumps(call('POST','/register',{'name':sys.argv[3]}),ensure_ascii=False))
elif op == 'post':
    token=(root/sys.argv[3]).read_text(encoding='utf-8').strip()
    print(json.dumps(call('POST','/post',{'token':token,'content':sys.stdin.read()}),ensure_ascii=False))
elif op == 'poll':
    print(json.dumps(call('GET','/poll?since_id='+sys.argv[3]+'&limit=50'),ensure_ascii=False))
elif op == 'count':
    print(json.dumps(call('GET','/count'),ensure_ascii=False))
else: raise SystemExit('unknown operation')
PY
chmod 600 "$BBS_CWD/bbs_client.py"

# 实测兼容性：`POST /post` 成功响应只含 `id` 和 `author`，不含帖子 `content`；客户端和断言应以实际响应 schema 为准，不要假设回显正文。

uv run python "$BBS_CWD/bbs_client.py" "$BBS_URL" register hive-master > "$BBS_CWD/master.register.json"
uv run python - "$BBS_CWD/master.register.json" "$BBS_CWD/master.token" <<'PY'
import json,pathlib,sys
p,o=map(pathlib.Path,sys.argv[1:]); d=json.loads(p.read_text()); assert d.get('token'); o.write_text(d['token']+'\n')
PY
chmod 600 "$BBS_CWD/master.register.json" "$BBS_CWD/master.token"
```

第一帖必须在任何 worker 启动前发送。用户目标取自 `objective.txt`；Hive Master 职责四点从已核验的 duty 文件自动提取，避免手抄漂移：

```bash
uv run python - "$BBS_CWD/first_post.txt" "$OBJECTIVE_FILE" "$DUTY_FILE" "$BBS_CWD" <<'PY'
import pathlib,sys
out,objective,duty,cwd=map(pathlib.Path,sys.argv[1:])
text=objective.read_text(encoding='utf-8').strip(); d=duty.read_text(encoding='utf-8').strip()
first=(f'任务目标：\n{text}\n\nHive Master 职责（完整规范见 {duty}）：\n{d}\n\n'
       f'工作目录：{cwd}（优先使用该目录传输文件，不使用 BBS 文件功能）\n\n'
       '此为最终目标，worker不要接单，先等hive master拆分子任务。')
out.write_text(first+'\n',encoding='utf-8')
PY
uv run python "$BBS_CWD/bbs_client.py" "$BBS_URL" post master.token < "$BBS_CWD/first_post.txt" > "$BBS_CWD/first_post.response.json"
chmod 600 "$BBS_CWD/first_post.txt" "$BBS_CWD/first_post.response.json"
```

### 5.1 先启动 worker，并等待冷启动游标就绪

Master 的第一条派单必须晚于 worker 的冷启动基线。否则 worker 在首次 `init()` 时用 `poll(0)` 记录已有帖子最大 id，可能把 Master 已经发出的任务当作历史帖子吞掉。首帖发送后，先为每个计划 worker 注册、启动并通过就绪闸门；本轮最多 5 个，通常 2—4 个足够。

```bash
worker_name=hive-worker-1
worker_dir="$BBS_CWD/$worker_name"; mkdir -p "$worker_dir"
uv run python "$BBS_CWD/bbs_client.py" "$BBS_URL" register "$worker_name" > "$worker_dir/register.json"
uv run python - "$worker_dir/register.json" "$worker_dir/token" <<'PY'
import json,pathlib,sys
p,o=map(pathlib.Path,sys.argv[1:])
d=json.loads(p.read_text(encoding='utf-8'))
assert d.get('token'), 'register response has no token'
o.write_text(d['token']+'\n',encoding='utf-8')
PY
cat > "$worker_dir/poll_reflect.py" <<'PY'
import json,pathlib,subprocess
INTERVAL=5; ONCE=False
root=pathlib.Path(__file__).parent; state=root/'poll_state.json'; base=''
def poll(since):
    return json.loads(subprocess.check_output(
        ['uv','run','python',str(root.parent/'bbs_client.py'),base,'poll',str(since)],
        text=True,timeout=20))
def init(a):
    global base
    base=a['base_url']
    if not state.exists():
        rows=poll(0)
        last_id=rows[-1]['id'] if rows else 0
        state.write_text(json.dumps({'last_id':last_id},ensure_ascii=False),encoding='utf-8')
def check():
    d=json.loads(state.read_text(encoding='utf-8')); rows=poll(d['last_id'])
    if not rows: return None
    d['last_id']=rows[-1]['id']
    state.write_text(json.dumps(d,ensure_ascii=False),encoding='utf-8')
    # 只让 hive-master 唤醒 worker；其他 worker 和自身帖子不会形成回声。
    useful=[r for r in rows if r['author']=='hive-master']
    if not useful: return None
    return ('你是 '+root.name+'。只执行 master 明确分派给你的任务或追加指令；不回应 ACK、说明帖或其他 worker。'
            '产物必须真实落盘，并回报路径和验证证据。\n新消息：\n'+
            '\n'.join(f"#{r['id']} {r['author']}: {r['content']}" for r in useful))
PY
chmod 600 "$worker_dir"/*.py "$worker_dir"/token "$worker_dir"/register.json
nohup uv run python "$CODE_ROOT/agentmain.py" \
  --reflect "$worker_dir/poll_reflect.py" --base_url "$BBS_URL" \
  > "$worker_dir/worker.log" 2>&1 &
worker_pid=$!
printf '%s\n' "$worker_pid" > "$BBS_CWD/$worker_name.pid"

# 就绪 = 仍是本轮 worker 进程、命令行指向本轮 reflector，且 init 已写出合法基线游标。
ready_deadline=$((SECONDS+60))
while :; do
  ready=0
  if [ -r "/proc/$worker_pid/cmdline" ]; then
    cmd=$(tr '\0' ' ' < "/proc/$worker_pid/cmdline")
    case "$cmd" in
      *"$CODE_ROOT/agentmain.py"*"$worker_dir/poll_reflect.py"*)
        if uv run python - "$worker_dir/poll_state.json" <<'PY' >/dev/null 2>&1
import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
d=json.loads(p.read_text(encoding='utf-8'))
assert isinstance(d.get('last_id'),int) and d['last_id'] >= 0
PY
        then ready=1; fi
        ;;
    esac
  fi
  [ "$ready" -eq 1 ] && break
  if [ "$SECONDS" -ge "$ready_deadline" ]; then
    echo "worker did not become ready: $worker_name" >&2
    if [ -r "/proc/$worker_pid/cmdline" ]; then
      cmd=$(tr '\0' ' ' < "/proc/$worker_pid/cmdline")
      case "$cmd" in
        *"$CODE_ROOT/agentmain.py"*"$worker_dir/poll_reflect.py"*)
          kill -TERM "$worker_pid" 2>/dev/null || true ;;
      esac
    fi
    exit 1
  fi
  sleep 1
done
printf 'worker ready: %s pid=%s baseline=%s\n' "$worker_name" "$worker_pid" \
  "$(uv run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["last_id"])' "$worker_dir/poll_state.json")"
```

只有上述就绪检查成功后，才创建并校验 `goal_state.json`，再启动 Master。`goal_state.start_time` 必须在启动前写入正数时间戳；留为 `null` 会使 `goal_mode` 的时间预算计算失败，Master 无法正常推进。这样 Master 的派单一定产生在 worker 的基线之后：

```bash
uv run python - "$OBJECTIVE_FILE" "$DUTY_FILE" "$BBS_URL" "$BBS_CWD/goal_state.json" <<'PY'
import json,pathlib,sys,time
objective,duty,url,out=sys.argv[1:]
text=pathlib.Path(objective).read_text(encoding='utf-8').strip()
d={
 'objective':f'{text}\nBBS_URL: {url}\n阅读 {duty}；你是 Hive Master，只负责拆解、调度、验收和汇总，不亲自生产子任务产物。',
 'budget_seconds':7200, 'max_turns':100, 'status':'running', 'turns_used':0,
 'start_time':time.time(),
 'done_prompt':'关闭所有你拉起的worker，并在BBS发一条帖子宣告你管理的任务结束，worker除了明确追加任务外，不应再回应。'}
pathlib.Path(out).write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
PY
uv run python - "$BBS_CWD/goal_state.json" <<'PY'
import json,sys

d=json.load(open(sys.argv[1],encoding='utf-8'))
assert d['status']=='running' and d['objective'] and d['budget_seconds']>0 and d['max_turns']>0
assert isinstance(d['start_time'],(int,float)) and d['start_time']>0
assert d['done_prompt']=='关闭所有你拉起的worker，并在BBS发一条帖子宣告你管理的任务结束，worker除了明确追加任务外，不应再回应。'
print('goal_state: OK')
PY
nohup uv run python "$CODE_ROOT/agentmain.py" \
  --reflect "$CODE_ROOT/reflect/goal_mode.py" \
  --goal_state "$BBS_CWD/goal_state.json" > "$BBS_CWD/master.log" 2>&1 &
printf '%s\n' "$!" > "$BBS_CWD/master.pid"
```

## 6. 扩容 worker（兼容 `/poll` 的 reflector）

需要更多 worker 时，重复上述注册和启动步骤并使用新的 `worker_name`；不要只复制旧 PID 或 token。扩容 worker 也必须先完成 `poll_state.json` 就绪闸门，再让 Master 发布面向它的明确新任务。由于 `/poll` 的冷启动会基线当时已有全部帖子，扩容期间 Master 不应先发给该 worker 的任务；若无法暂停调度，就绪后必须由 Master 重发一次明确、可幂等的任务，并在验收时去重。

冷启动按 id 升序读取，每批最多 50 条；积压超过 50 条时由后续检查继续排空。worker 只响应 `author == 'hive-master'` 的新增帖子，不回应首帖、ACK、其他 worker 或自身帖子。

## 7. 运行中检查

至少检查三项：

```bash
# 服务（key 由私有客户端读取，不进入命令行）
uv run python "$BBS_CWD/bbs_client.py" "$BBS_URL" count

# 状态
uv run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$BBS_CWD/goal_state.json"

# 进程及错误
ps -o pid,ppid,stat,etime,cmd -p "$(cat "$BBS_CWD/master.pid")" \
  "$(cat "$BBS_CWD"/worker-*.pid 2>/dev/null)" 2>/dev/null || true
tail -50 "$BBS_CWD/master.log"
```

不要仅凭 BBS 回帖、pid 文件或 `goal_state.status` 判断完成；必须回读实际产物，并核对 `/proc/<pid>/cmdline`。

## 8. 收口

1. 核验核心产物、证据、缺口和未验证项；
2. master 发明确收口帖；
3. **自动清场（reflect/goal_mode.py `_shutdown_hive`）**：master 的 `check()` 发现 `goal_state.status != running`（done/done_budget/手动终态）时，读 BBS_CWD 下 `bbs.pid` / `hive-worker-*.pid`，并经 `/proc/<pid>/task/<pid>/children` 递归覆盖 uv壳+python 双层子进程，TERM → 2s → 存活者 KILL 兜底，全部终止；master 自身走 `/exit` 退出。审计数据保留在磁盘（bbs_files / boards.json / *.log），进程停止不影响审计；
4. 兜底：若自动清场未生效（master 被强杀、进程未写 pid 文件等），手动精确终止——读 BBS_CWD 下 pid 文件逐个 TERM（含子进程），2s 后核对 `/proc` 残留再 KILL；不无条件杀 Python；BBS 数据在磁盘可随时按既有启动方式恢复（`agent_bbs.py --port <PORT>`，cwd=BBS_CWD）；
5. 终止后再次验证目标进程已消失（`ps` + `ss -tlnp` 端口无监听）。

示例（只按精确 argv 验证本轮 Master/worker；不匹配则拒绝终止）：

```bash
# 检查某个 argv 是否作为完整参数出现，避免路径顺序或子串误判。
arg_present() {
  local pid=$1 want=$2
  [ -r "/proc/$pid/cmdline" ] || return 1
  tr '\0' '\n' < "/proc/$pid/cmdline" |
    grep -F -x -- "$want" >/dev/null 2>&1
}
valid_pid() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$1" -gt 0 ] 2>/dev/null
}
master_cmdline_ok() {
  local pid=$1
  arg_present "$pid" "$CODE_ROOT/agentmain.py" &&
  arg_present "$pid" --reflect &&
  arg_present "$pid" "$CODE_ROOT/reflect/goal_mode.py" &&
  arg_present "$pid" --goal_state &&
  arg_present "$pid" "$BBS_CWD/goal_state.json"
}
worker_cmdline_ok() {
  local pid=$1 worker_dir=$2
  arg_present "$pid" "$CODE_ROOT/agentmain.py" &&
  arg_present "$pid" --reflect &&
  arg_present "$pid" "$worker_dir/poll_reflect.py" &&
  arg_present "$pid" --base_url &&
  arg_present "$pid" "$BBS_URL"
}

terminate_master() {
  local f="$BBS_CWD/master.pid" pid
  [ -f "$f" ] || return 0
  pid=$(cat "$f")
  if valid_pid "$pid" && master_cmdline_ok "$pid"; then
    echo "terminating verified master pid=$pid"
    kill -TERM "$pid" 2>/dev/null || true
  else
    echo "refusing to terminate unverified master pid=$pid" >&2
  fi
}
terminate_worker() {
  local f=$1 pid worker_name worker_dir
  [ -f "$f" ] || return 0
  pid=$(cat "$f"); worker_name=$(basename "$f" .pid)
  worker_dir="$BBS_CWD/$worker_name"
  if valid_pid "$pid" && worker_cmdline_ok "$pid" "$worker_dir"; then
    echo "terminating verified worker=$worker_name pid=$pid"
    kill -TERM "$pid" 2>/dev/null || true
  else
    echo "refusing to terminate unverified worker=$worker_name pid=$pid" >&2
  fi
}
terminate_master
for f in "$BBS_CWD"/worker-*.pid; do
  [ -f "$f" ] || continue
  terminate_worker "$f"
done
sleep 2
```

终止后再次按角色验证；PID 文件存在不等于进程仍属于本轮：

```bash
check_master() {
  local f="$BBS_CWD/master.pid" pid
  [ -f "$f" ] || return 0
  pid=$(cat "$f")
  if valid_pid "$pid" && master_cmdline_ok "$pid"; then
    echo "残留: verified master pid=$pid" >&2
  else
    echo "master target gone or command changed: pid=$pid"
  fi
}
check_worker() {
  local f=$1 pid worker_name worker_dir
  [ -f "$f" ] || return 0
  pid=$(cat "$f"); worker_name=$(basename "$f" .pid)
  worker_dir="$BBS_CWD/$worker_name"
  if valid_pid "$pid" && worker_cmdline_ok "$pid" "$worker_dir"; then
    echo "残留: verified worker=$worker_name pid=$pid" >&2
  else
    echo "worker target gone or command changed: $worker_name pid=$pid"
  fi
}
check_master
for f in "$BBS_CWD"/worker-*.pid; do
  [ -f "$f" ] || continue
  check_worker "$f"
done
```


未落盘或仅写“待验证”的内容必须列为残差，不能当成交付结果。

## 短预算任务的外部干预（2026-08-09 实测）
30 分钟短预算下，Master 可能前 2/3 预算自己深挖资料（读源码/官方文档）而零派发，
worker 全空闲、turns 仅个位数 → 判定“被细节牵走丢全局”失稳。
处置：外部 orchestration 用 `hive-master` 的 token 直接发任务帖（worker 只响应
author==hive-master 的帖子）：`printf ... | uv run python "$BBS_CWD/bbs_client.py" "$BBS_URL" post master.token`，
帖内写明目标 worker、任务、deadline。实测干预后 worker 3 分钟内完成并回帖。
观察 Master 动向看 `$BBS_CWD/master.log` 尾部；temp/model_responses 会被并发任务污染，不可作判据。
Master 收口后可能卡在 LLM 重试不发宣告帖、不关 worker，外部按 terminate 段核验后代发宣告帖并收口。
