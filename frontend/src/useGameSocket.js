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
          setSnapshot(msg);
          setClockOffset(msg.server_now - Date.now() / 1000);
        } else if (msg.type === "pick_ack" || msg.type === "change_ack" || msg.type === "error") {
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

  // Re-select a card for a pick already submitted this round. The server
  // answers with change_ack (and a fresh snapshot) or an error; a dropped
  // socket is covered by the reconnect snapshot like with picks.
  const sendChange = useCallback((cardId, round, expectedRevision) => {
    const clientMsgId = crypto.randomUUID();
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(
        JSON.stringify({
          type: "change_pick",
          card: cardId,
          round,
          expected_revision: expectedRevision,
          client_msg_id: clientMsgId,
        })
      );
    }
    return new Promise((resolve) => {
      pendingRef.current.set(clientMsgId, resolve);
      setTimeout(() => {
        if (pendingRef.current.delete(clientMsgId)) resolve(null);
      }, 5000);
    });
  }, []);

  return { snapshot, connected, clockOffset, sendPick, sendChange };
}
