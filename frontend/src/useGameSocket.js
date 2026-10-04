import { useEffect, useRef, useState, useCallback } from "react";

/**
 * WebSocket connection with auto-reconnect. The server sends a personalized
 * snapshot on every state change (and right after each (re)connect), so the
 * UI only ever renders the latest snapshot — reconnects are seamless and
 * never expose anything beyond the server's per-player view.
 */
export function useGameSocket(gameId, token) {
  const [snapshot, setSnapshot] = useState(null);
  const [connected, setConnected] = useState(false);
  const [clockOffset, setClockOffset] = useState(0);
  const wsRef = useRef(null);
  const pendingRef = useRef(new Map()); // client_msg_id -> resolve
  const progressRef = useRef({ status: "lobby", round: 0 }); // newest applied snapshot

  useEffect(() => {
    if (!gameId || !token) return;
    let closed = false;
    let retryTimer = null;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws/${gameId}?token=${token}`);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "snapshot") {
          // Snapshots for the same seat can race each other (e.g. a
          // change-pick snapshot built just before a reveal but delivered
          // after it). Never let an older round replace a newer one.
          const prev = progressRef.current;
          const stale =
            (prev.status === "finished" && msg.status !== "finished") ||
            (prev.status === "active" && msg.status === "active" && msg.round < prev.round);
          if (stale) return;
          progressRef.current = { status: msg.status, round: msg.round };
          setSnapshot(msg);
          setClockOffset(msg.server_now - Date.now() / 1000);
        } else if (
          msg.type === "pick_ack" ||
          msg.type === "change_pick_ack" ||
          msg.type === "error"
        ) {
          const resolve = pendingRef.current.get(msg.client_msg_id);
          if (resolve) {
            pendingRef.current.delete(msg.client_msg_id);
            resolve(msg);
          }
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retryTimer = setTimeout(connect, 1000);
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(retryTimer);
      wsRef.current?.close();
    };
  }, [gameId, token]);

  const sendPick = useCallback((cardId, round) => {
    const clientMsgId = crypto.randomUUID();
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "pick", card: cardId, round, client_msg_id: clientMsgId }));
    }
    return new Promise((resolve) => {
      pendingRef.current.set(clientMsgId, resolve);
      // If the socket dropped, the reconnect snapshot will reflect the pick
      // anyway (submissions are idempotent server-side).
      setTimeout(() => {
        if (pendingRef.current.delete(clientMsgId)) resolve(null);
      }, 5000);
    });
  }, []);

  // Private re-pick before the round is revealed. Carries the revision the
  // client based its decision on; the server rejects stale revisions.
  const sendChangePick = useCallback((cardId, round, revision) => {
    const clientMsgId = crypto.randomUUID();
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(
        JSON.stringify({
          type: "change_pick",
          card: cardId,
          round,
          revision,
          client_msg_id: clientMsgId,
        })
      );
    }
    return new Promise((resolve) => {
      pendingRef.current.set(clientMsgId, resolve);
      // On a dropped socket the reconnect snapshot carries the authoritative
      // pick + revision, so the UI resyncs without this ack.
      setTimeout(() => {
        if (pendingRef.current.delete(clientMsgId)) resolve(null);
      }, 5000);
    });
  }, []);

  return { snapshot, connected, clockOffset, sendPick, sendChangePick };
}
