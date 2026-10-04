export async function createGame() {
  const r = await fetch("/api/games", { method: "POST" });
  if (!r.ok) throw new Error("创建对局失败");
  return r.json();
}

export async function joinGame(gameId, name) {
  const r = await fetch(`/api/games/${gameId}/join`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  const body = await r.json();
  if (!r.ok) throw new Error(body.message || body.error || "加入失败");
  return body;
}
