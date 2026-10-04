/**
 * Frontend: pick → private change-pick → disconnect/reconnect.
 *
 * A scripted MockWebSocket stands in for the server. The test drives the
 * real App component: submit a pick, change it (the request must carry the
 * round, the expected revision and the new card id), then drop the socket
 * and verify that the reconnect snapshot restores the changed pick — the
 * UI renders only what the server's personalized snapshot says.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { randomUUID } from "node:crypto";
import App from "./App.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
if (!globalThis.crypto?.randomUUID) {
  Object.defineProperty(globalThis, "crypto", { value: { randomUUID } });
}

const PACK = [
  { id: "c01", name: "Ember Sprite", power: 1 },
  { id: "c02", name: "Tide Caller", power: 2 },
  { id: "c03", name: "Stone Sentinel", power: 3 },
  { id: "c04", name: "Gale Dancer", power: 4 },
  { id: "c05", name: "Thorn Witch", power: 5 },
];

function snapshot(over = {}) {
  const pick = over.your_pick ?? null;
  return {
    type: "snapshot",
    game_id: "abcd1234",
    status: "active",
    you: { seat: 0, name: "Ann" },
    players: [0, 1, 2, 3].map((s) => ({
      seat: s,
      name: `P${s}`,
      connected: true,
      submitted: s === 0 ? !!pick : false,
    })),
    round: 1,
    rounds_total: 5,
    your_pack: PACK,
    your_pick: pick,
    your_pick_auto: false,
    your_pick_revision: pick ? (over.your_pick_revision ?? 1) : null,
    can_change_pick: !!pick,
    your_picks_all: [],
    revealed: [],
    deadline: 1_700_000_030,
    server_now: 1_700_000_000,
    timeout_seconds: 30,
    ...over,
  };
}

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.sent = [];
    this.readyState = MockWebSocket.CONNECTING;
    MockWebSocket.instances.push(this);
    // Open on a microtask: the hook attaches its handlers right after
    // construction, so a synchronous open would fire before they exist.
    queueMicrotask(() => {
      if (this.readyState === MockWebSocket.CONNECTING) {
        this.readyState = MockWebSocket.OPEN;
        this.onopen?.();
      }
    });
  }

  send(data) {
    this.sent.push(JSON.parse(data));
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }

  // -- test-side server controls --------------------------------------
  receive(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }

  drop() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }

  lastSent() {
    return this.sent[this.sent.length - 1];
  }
}

function cardButton(name) {
  return screen.getByText(name).closest("button");
}

beforeEach(() => {
  MockWebSocket.instances = [];
  globalThis.WebSocket = MockWebSocket;
  sessionStorage.clear();
  sessionStorage.setItem(
    "draft.session",
    JSON.stringify({ gameId: "abcd1234", token: "tok", seat: 0, name: "Ann" })
  );
});

describe("change pick + reconnect", () => {
  it("submits, privately changes the pick, and restores it after reconnect", async () => {
    render(<App />);
    await act(async () => {}); // flush the socket-open microtask
    const ws = MockWebSocket.instances[0];
    expect(ws.url).toBe("ws://localhost:3000/ws/abcd1234?token=tok");
    expect(ws.readyState).toBe(MockWebSocket.OPEN);

    // Server pushes the round-1 snapshot: five cards to pick from.
    act(() => ws.receive(snapshot()));
    expect(cardButton("Ember Sprite").disabled).toBe(false);

    // First pick: Tide Caller.
    fireEvent.click(cardButton("Tide Caller"));
    expect(ws.lastSent()).toMatchObject({ type: "pick", card: "c02", round: 1 });
    act(() => {
      ws.receive({
        type: "pick_ack",
        round: 1,
        card: "c02",
        auto: false,
        already: false,
        client_msg_id: ws.lastSent().client_msg_id,
      });
      ws.receive(snapshot({ your_pick: PACK[1], your_pick_revision: 1 }));
    });
    expect(screen.getByText(/已选择 Tide Caller/)).toBeTruthy();
    expect(cardButton("Tide Caller").className).toContain("selected");

    // Changed my mind: click Gale Dancer. The request must carry the round,
    // the expected revision and the new card id.
    fireEvent.click(cardButton("Gale Dancer"));
    expect(ws.lastSent()).toMatchObject({
      type: "change_pick",
      card: "c04",
      round: 1,
      revision: 1,
    });
    act(() => {
      ws.receive({
        type: "change_pick_ack",
        round: 1,
        card: "c04",
        revision: 2,
        already: false,
        client_msg_id: ws.lastSent().client_msg_id,
      });
      ws.receive(snapshot({ your_pick: PACK[3], your_pick_revision: 2 }));
    });
    expect(screen.getByText(/已选择 Gale Dancer/)).toBeTruthy();
    expect(cardButton("Gale Dancer").className).toContain("selected");
    expect(cardButton("Tide Caller").className).not.toContain("selected");

    // 断线重连: the socket drops, the hook reconnects by itself, and the
    // fresh server snapshot restores the CHANGED pick (not the old one).
    act(() => ws.drop());
    expect(screen.getByTitle("断线重连中")).toBeTruthy();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 1200)); // reconnect timer: 1s
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    const ws2 = MockWebSocket.instances[1];
    act(() => ws2.receive(snapshot({ your_pick: PACK[3], your_pick_revision: 2 })));

    expect(screen.getByTitle("已连接")).toBeTruthy();
    expect(screen.getByText(/已选择 Gale Dancer/)).toBeTruthy();
    expect(cardButton("Gale Dancer").className).toContain("selected");
    // The pack is still there for further changes, and no stale "pick"
    // message is re-sent on reconnect.
    expect(cardButton("Ember Sprite").disabled).toBe(false);
    expect(ws2.sent).toHaveLength(0);
  });

  it("offers no change button for auto (timeout) picks", async () => {
    render(<App />);
    await act(async () => {}); // flush the socket-open microtask
    const ws = MockWebSocket.instances[0];
    act(() =>
      ws.receive(
        snapshot({
          your_pick: PACK[0],
          your_pick_auto: true,
          your_pick_revision: 1,
          can_change_pick: false,
        })
      )
    );
    expect(screen.getByText(/超时自动/)).toBeTruthy();
    for (const card of PACK) {
      expect(cardButton(card.name).disabled).toBe(true);
    }
    expect(ws.sent).toHaveLength(0);
  });

  it("ignores a stale snapshot that arrives after a newer round", async () => {
    render(<App />);
    await act(async () => {});
    const ws = MockWebSocket.instances[0];
    // Round 2 snapshot first (the reveal already happened)…
    act(() =>
      ws.receive(
        snapshot({
          round: 2,
          your_pack: PACK.slice(0, 4),
          revealed: [
            {
              round: 1,
              picks: { 0: PACK[0], 1: PACK[1], 2: PACK[2], 3: PACK[3] },
              auto: [],
            },
          ],
        })
      )
    );
    expect(screen.getByText(/第 2\/5 轮/)).toBeTruthy();
    // …then a stale round-1 change-pick snapshot delivered out of order.
    act(() =>
      ws.receive(snapshot({ your_pick: PACK[1], your_pick_revision: 2 }))
    );
    // The UI must stay on round 2 and must not resurrect the round-1 pick.
    expect(screen.getByText(/第 2\/5 轮/)).toBeTruthy();
    expect(screen.queryByText(/已选择 Tide Caller/)).toBeNull();
  });
});
