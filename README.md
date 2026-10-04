# 四人模拟卡牌轮抽

FastAPI + WebSocket + SQLite 后端，React 前端。四名玩家各持一包牌，每轮秘密选一张；
全员提交或时钟超时后，服务器**同时公开**本轮所有选择，余牌传给下一位，五轮后结束。

## 规则

- 4 名玩家，20 张牌，每人起手一包 5 张。
- 每轮每人从当前包中秘密选 1 张；第 N 轮包内牌数为 6-N（5→4→3→2→1）。
- 4 人全部提交、或轮次截止时间到（未提交者按**稳定规则**自动选：包内 id 最小的牌），
  服务器才同时公开本轮全部选择，并把每人的余牌传给下一个座位（seat+1）。
- 揭晓前可**改选**：已手动提交的座位可私下换选本轮原包中的另一张牌
  （请求带轮次、预期修订号与新牌 id，修订号匹配才生效）；超时自动选择不可改。
  其他玩家始终只能看到"这名玩家已提交"。
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
python -m pytest            # 29 个测试

cd frontend
npm install
npm test                    # 4 个前端测试（vitest + jsdom）
```

覆盖：

- `test_concurrency.py` — 重复点击 20 次只记 1 次；4 人同一瞬时并发提交只产生 1 次揭晓；
  每轮"第 4 人提交 vs 超时"并发竞争恰好结算一次；断线重连后重复提交幂等；
  轮次结束后迟到的提交返回幂等 ack 而不改状态。
- `test_change_pick.py` — 改选：成功改选在同一持久提交中更新选择、递增修订并追加
  `pick_changed` 事件，揭晓与传牌都用新牌；未提交/非原包牌/修订号冲突/轮次已过时
  均拒绝；超时自动选牌不可改；可控时钟 + 并发屏障核对"改选 vs 第 4 人提交"
  与"改选 vs 超时揭晓"两种竞争（锁裁决，先到先赢，结果恰结算一次）；重启重放恢复
  最新待选牌与修订号；其他玩家的 WS 消息、HTTP 快照与错误响应不含改选前后牌面。
- `test_hidden_info.py` — 以服务器私有事件日志为基准，逐条扫描每个客户端实际收到的
  所有 WebSocket 消息、重连快照（WS 与 HTTP 两条路径）和错误响应，断言不含他人
  当前包与未公开选择的任何牌。
- `test_persistence.py` — 中途停服重启后待选轮次完整恢复（已提交的选择、剩余手牌）；
  终局结果重启后回放一致；停机期间过期的轮次在恢复后立即按稳定规则结算。
- `test_clock.py` / `test_flow.py` / `test_store.py` — 自动时钟从第 1 轮起驱动整局；
  传牌方向与牌数守恒；picks 表唯一约束兜底；改选的条件 UPDATE（修订号 + 非自动）
  作为数据库层兜底，旧库自动迁移补 revision 列。
- 前端 `App.test.jsx` — 断线重连后仍显示原包与当前选择、可在新连接上改选；
  改选请求携带轮次/预期修订/新牌并按 ack 更新；未提交时仍走首次 pick；
  超时自动选择不提供改选入口。

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
  （WS 推送、WS 重连快照、HTTP `/state` 共用），只含自己的当前包、自己的本轮原包
  （`your_original_pack`，供改选）、自己的提交状态与修订号、已公开的历史选择；
  错误消息只有错误码，不含任何牌数据。已提交后 `your_pack` 置空。
- **幂等与并发**：每局一把 `asyncio.Lock` 串行化所有变更；同一座位同一轮首次记录生效，
  重复/迟到/重连后的提交一律返回已记录的牌（`already: true`）；SQLite `picks` 表
  `UNIQUE(game_id, round, seat)` 作为最后防线。
- **改选**：仅"已手动提交且当前轮未揭晓"的座位可用；请求带轮次、预期修订号
  （`expected_revision`，乐观并发控制）与新牌 id，新牌必须属于该座位本轮原包。
  成功时更新 picks 行、递增修订并追加 `pick_changed` 事件，三者同事务提交；
  改选与第 4 人提交/超时揭晓走同一把锁——改选先提交则揭晓用新牌，揭晓先提交则
  改选失败且无副作用。超时自动选牌不可改（内存与数据库双重把关）。改选只通知本人，
  其他玩家收不到任何消息；重启重放 `pick_changed` 事件恢复最新待选牌。
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
| WS | `/ws/{id}?token=` | 推送个性化快照；收 `{"type":"pick","card","round","client_msg_id"}` 与 `{"type":"change_pick","card","round","expected_revision","client_msg_id"}` |

WS 下行消息：`snapshot`（个性化状态，含 `your_original_pack` 与 `your_pick_revision`）、
`pick_ack`（含 `already` 幂等标记）、`change_ack`（含新 `revision`）、
`error`（仅错误码）、`pong`。
