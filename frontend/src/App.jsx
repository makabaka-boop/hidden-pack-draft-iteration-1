import React, { useEffect, useMemo, useState } from "react";
import { createGame, joinGame } from "./api.js";
import { useGameSocket } from "./useGameSocket.js";

// Identity lives in sessionStorage: each browser tab is its own player,
// so you can demo a 4-player game by opening four tabs.
function loadSession() {
  try {
    return JSON.parse(sessionStorage.getItem("draft.session")) || null;
  } catch {
    return null;
  }
}

export default function App() {
  const [session, setSession] = useState(loadSession);
  useEffect(() => {
    if (session) sessionStorage.setItem("draft.session", JSON.stringify(session));
  }, [session]);

  if (!session) return <JoinScreen onJoined={setSession} />;
  return (
    <GameScreen
      session={session}
      onLeave={() => {
        sessionStorage.removeItem("draft.session");
        setSession(null);
      }}
    />
  );
}

function JoinScreen({ onJoined }) {
  const [name, setName] = useState("");
  const [gameId, setGameId] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const join = async (id) => {
    setBusy(true);
    setError("");
    try {
      const res = await joinGame(id, name.trim());
      onJoined({ gameId: res.game_id, token: res.token, seat: res.seat, name: res.name });
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="join-screen">
      <h1>四人卡牌轮抽</h1>
      <p className="muted">每轮秘密选一张牌，全员提交或超时后同时公开，余牌传给下一位。</p>
      <label>
        昵称
        <input
          value={name}
          maxLength={24}
          placeholder="你的名字"
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <div className="row">
        <button
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              const g = await createGame();
              await join(g.game_id);
            } catch (e) {
              setError(e.message);
              setBusy(false);
            }
          }}
        >
          创建新对局并加入
        </button>
      </div>
      <div className="row">
        <input
          value={gameId}
          placeholder="对局号（8位）"
          onChange={(e) => setGameId(e.target.value.trim())}
        />
        <button disabled={busy || gameId.length < 4} onClick={() => join(gameId)}>
          加入对局
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <p className="muted small">提示：开 4 个标签页分别加入同一对局号即可本地开局。</p>
    </div>
  );
}

function GameScreen({ session, onLeave }) {
  const { snapshot, connected, clockOffset, sendPick } = useGameSocket(
    session.gameId,
    session.token
  );
  const [pendingCard, setPendingCard] = useState(null);
  const [now, setNow] = useState(Date.now() / 1000);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 250);
    return () => clearInterval(t);
  }, []);

  // Clear the optimistic marker once the server confirms our pick.
  useEffect(() => {
    if (snapshot?.your_pick) setPendingCard(null);
  }, [snapshot?.your_pick, snapshot?.round]);

  const remaining = useMemo(() => {
    if (!snapshot?.deadline) return null;
    return Math.max(0, snapshot.deadline - (now + clockOffset));
  }, [snapshot, now, clockOffset]);

  if (!snapshot) return <div className="join-screen">连接中…</div>;

  const me = snapshot.you;
  const picked = snapshot.your_pick || (pendingCard ? { id: pendingCard } : null);
  const canPick = snapshot.status === "active" && !picked && snapshot.your_pack.length > 0;

  const pick = (cardId) => {
    if (!canPick) return;
    setPendingCard(cardId);
    sendPick(cardId, snapshot.round);
  };

  return (
    <div className="game-screen">
      <header>
        <div>
          <strong>对局 {snapshot.game_id}</strong>
          <span className="muted">　你是 {me.name}（座位 {me.seat + 1}）</span>
        </div>
        <div>
          {snapshot.status === "active" && (
            <span className={remaining !== null && remaining < 5 ? "timer urgent" : "timer"}>
              第 {snapshot.round}/{snapshot.rounds_total} 轮 · {remaining?.toFixed(0)}s
            </span>
          )}
          {snapshot.status === "finished" && <span className="timer">对局结束</span>}
          <span className={connected ? "dot on" : "dot off"} title={connected ? "已连接" : "断线重连中"} />
          <button className="link" onClick={onLeave}>离开</button>
        </div>
      </header>

      {snapshot.status === "lobby" && (
        <section className="panel">
          <h2>等待玩家加入（{snapshot.players.length}/4）</h2>
          <p className="muted">把对局号 <code>{snapshot.game_id}</code> 告诉其他玩家，满 4 人自动开始。</p>
        </section>
      )}

      {snapshot.status !== "lobby" && (
        <>
          <section className="players">
            {snapshot.players.map((p) => (
              <div key={p.seat} className={"player" + (p.seat === me.seat ? " me" : "")}>
                <span className={p.connected ? "dot on" : "dot off"} />
                {p.name}
                {snapshot.status === "active" && (
                  <span className={"flag" + (p.submitted ? " ok" : "")}>
                    {p.submitted ? "已提交" : "思考中"}
                  </span>
                )}
              </div>
            ))}
          </section>

          {snapshot.status === "active" && (
            <section className="panel">
              <h2>
                {picked
                  ? `已选择 ${picked.name || picked.id}${snapshot.your_pick_auto ? "（超时自动）" : ""}，等待其他玩家…`
                  : "选择一张牌"}
              </h2>
              <div className="pack">
                {snapshot.your_pack.map((card) => (
                  <button
                    key={card.id}
                    className="card"
                    disabled={!canPick}
                    onClick={() => pick(card.id)}
                  >
                    <span className="card-name">{card.name}</span>
                    <span className="card-meta">力量 {card.power} · {card.id}</span>
                  </button>
                ))}
                {snapshot.your_pack.length === 0 && (
                  <p className="muted">本轮已提交，公开后自动进入下一轮。</p>
                )}
              </div>
            </section>
          )}

          {snapshot.status === "finished" && (
            <section className="panel">
              <h2>最终结果</h2>
              <FinalTable snapshot={snapshot} />
            </section>
          )}

          <section className="columns">
            <div className="panel">
              <h3>公开记录</h3>
              {snapshot.revealed.length === 0 && <p className="muted">还没有公开的轮次。</p>}
              {[...snapshot.revealed].reverse().map((r) => (
                <div key={r.round} className="round">
                  <strong>第 {r.round} 轮</strong>
                  <ul>
                    {Object.entries(r.picks).map(([seat, card]) => (
                      <li key={seat}>
                        {snapshot.players[Number(seat)]?.name ?? `座位${Number(seat) + 1}`}：
                        {card.name}
                        {r.auto.includes(Number(seat)) && <em>（自动）</em>}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
            <div className="panel">
              <h3>我的已选（{snapshot.your_picks_all.length}）</h3>
              <ul>
                {snapshot.your_picks_all.map((card, i) => (
                  <li key={card.id}>第 {i + 1} 轮：{card.name}（力量 {card.power}）</li>
                ))}
              </ul>
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function FinalTable({ snapshot }) {
  return (
    <table>
      <thead>
        <tr>
          <th>玩家</th>
          {Array.from({ length: snapshot.rounds_total }, (_, i) => (
            <th key={i}>第 {i + 1} 轮</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {snapshot.players.map((p) => (
          <tr key={p.seat}>
            <td>{p.name}</td>
            {snapshot.revealed.map((r) => (
              <td key={r.round}>
                {r.picks[String(p.seat)]?.name}
                {r.auto.includes(p.seat) && <em>（自动）</em>}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
