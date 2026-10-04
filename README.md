# 四人模拟卡牌轮抽

FastAPI + WebSocket + SQLite 后端，React 前端。四名玩家各持一包牌，每轮秘密选一张；
全员提交或时钟超时后，服务器**同时公开**本轮所有选择，余牌传给下一位，五轮后结束。

## 规则

- 4 名玩家，20 张牌，每人起手一包 5 张。
- 每轮每人从当前包中秘密选 1 张；第 N 轮包内牌数为 6-N（5→4→3→2→1）。
- 4 人全部提交、或轮次截止时间到（未提交者按**稳定规则**自动选：包内 id 最小的牌），
  服务器才同时公开本轮全部选择，并把每人的余牌传给下一个座位（seat+1）。
- **改选**：手动提交后、本轮揭晓前，可私下改选同包内另一张牌（请求带轮次、
  预期修订号和新牌 ID，服务器校验修订号乐观锁）；超时自动选择的牌不可改。
  其他玩家始终只能看到"该玩家已提交"，看不到改选前后的牌面。
- 五轮结束后可公开回放全部选牌结果。

## 运行

```bash
# 后端（Python 3.11+）
cd backend
pip install -r requirements.txt          # 或: uv pip install -r requirements.txt
python -m app.main                       # http://localhost:8000，SQLite 写入 ./draft.db

# 前端（Node 20+）：构建后由 FastAPI 直接托管
cd frontend
npm install
npm run build                            # 产物在 frontend/dist，刷新 http://localhost:8000 即可

# 前端开发模式（可选）：Vite 代理 /api 与 /ws 到 8000 端口
npm run dev
```

本地试玩：打开 4 个浏览器标签页，第一个创建对局，其余用对局号加入
（身份保存在 sessionStorage，每个标签页是独立玩家），满 4 人自动开始。

## 测试

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest            # 31 个测试

cd frontend
npm install
npm test                    # vitest：改选 + 断线重连
```

覆盖：

- `test_concurrency.py` — 重复点击 20 次只记 1 次；4 人同一瞬时并发提交只产生 1 次揭晓；
  每轮"第 4 人提交 vs 超时"并发竞争恰好结算一次；断线重连后重复提交幂等；
  轮次结束后迟到的提交返回幂等 ack 而不改状态。
- `test_change_pick.py` — 改选：同一持久提交内换牌 + 修订号递增 + 不可变事件；
  修订号冲突/非本包牌/未提交/已揭晓等错误路径不含任何牌面；丢 ack 重试幂等；
  用 `asyncio.Barrier` 同步"改选 vs 第 4 人提交""改选 vs 超时揭晓"竞争，
  两种锁序下结果都一致（改选先提交则揭晓用新牌，揭晓先提交则改选失败且不动传牌）；
  重启重放恢复最新待选牌与修订号；超时自动选牌锁定不可改；
  改选对其他玩家完全不可见（WS、HTTP 快照、错误响应三路核查）。
- `test_hidden_info.py` — 以服务器私有事件日志为基准，逐条扫描每个客户端实际收到的
  所有 WebSocket 消息、重连快照（WS 与 HTTP 两条路径）和错误响应，断言不含他人
  当前包与未公开选择的任何牌。
- `test_persistence.py` — 中途停服重启后待选轮次完整恢复（已提交的选择、剩余手牌）；
  终局结果重启后回放一致；停机期间过期的轮次在恢复后立即按稳定规则结算。
- `test_clock.py` / `test_flow.py` / `test_store.py` — 自动时钟从第 1 轮起驱动整局；
  传牌方向与牌数守恒；picks 表唯一约束与改选修订号兜底。
- `frontend/src/App.test.jsx` — 提交→改选→断线重连：重连快照恢复改选后的选择；
  超时自动选牌不显示改选入口。

## 架构

```
backend/app/
  cards.py   20 张牌定义
  store.py   SQLite：games / players / events（事件日志，真相之源）/ picks（唯一约束兜底）
  game.py    Game 状态机 + GameManager（每局一把 asyncio 锁、连接注册、时钟）
  main.py    FastAPI：HTTP API、WebSocket、静态托管
frontend/    React（Vite）：Join 页 + 对局页，全部渲染自服务器推送的个性化快照
```

### 关键设计

- **隐藏信息唯一出口**：所有发往客户端的状态只由 `Game.snapshot_for(seat)` 生成
  （WS 推送、WS 重连快照、HTTP `/state` 共用），只含自己的当前包、自己的提交状态、
  已公开的历史选择；错误消息只有错误码，不含任何牌数据。本人已提交后仍可看到自己的
  原包与当前选择（供改选），其他玩家只能看到"已提交"标记。
- **幂等与并发**：每局一把 `asyncio.Lock` 串行化所有变更；同一座位同一轮首次记录生效，
  重复/迟到/重连后的提交一律返回已记录的牌（`already: true`）；SQLite `picks` 表
  `UNIQUE(game_id, round, seat)` 作为最后防线。
- **改选**：仅"手动提交且本轮未揭晓"的座位可用；请求带轮次、预期修订号与新牌 ID，
  新牌必须属于该座位本轮原包。成功改选在**同一个 SQLite 事务**里更新 picks 行、
  递增修订号并追加不可变 `pick_changed` 事件；重启重放该事件恢复最新待选牌。
  改选与第 4 人提交/超时揭晓走同一把每局锁：先提交者生效，改选落败时返回
  `round_not_active` 且不触碰传牌与公开结果。改选只向本人推送新快照，
  其他玩家收不到任何消息；超时自动选牌（`auto`）锁定不可改。
- **可控时钟**：`clock_mode="auto"` 时每轮一个定时任务；`"manual"` 时只响应
  `POST /api/games/{id}/timeout`（测试与演示用）。超时与人工提交走同一把锁、
  同一个结算入口，竞争恰好结算一次。稳定自动选牌规则只依赖包内容，与时序无关。
- **持久化恢复**：所有状态变更先写事件日志再提交；重启时重放事件重建内存状态，
  待选轮次按剩余时间重新武装定时器（已过期则立即结算）；`/replay` 由公开事件
  重建最终选牌结果。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/games` | 创建对局 |
| POST | `/api/games/{id}/join` | 加入，返回 `{seat, token}`（第 4 人加入自动开局） |
| GET | `/api/games/{id}/state?token=` | 重连快照（与 WS 同一过滤函数） |
| POST | `/api/games/{id}/timeout` | 可控时钟：立即结算当前轮 |
| GET | `/api/games/{id}/replay` | 公开回放：各轮选择与最终结果 |
| WS | `/ws/{id}?token=` | 推送个性化快照；收 `{"type":"pick","card","round","client_msg_id"}` 与 `{"type":"change_pick","card","round","revision","client_msg_id"}` |

WS 下行消息：`snapshot`（个性化状态，含 `your_pick_revision`、`can_change_pick`）、
`pick_ack`（含 `already` 幂等标记）、`change_pick_ack`（含新 `revision`）、
`error`（仅错误码）、`pong`。
