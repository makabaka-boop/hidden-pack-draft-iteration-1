/**
 * Frontend change-pick (改选) tests, centered on disconnect/reconnect:
 * the server re-sends the personalized snapshot after a reconnect, so the
 * original pack and the current pick — and with them the change entry —
 * must survive a dropped socket. Also covers the change request shape
 * (round + expected revision + new card) and the locked auto-pick case.
 */
import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

// -- Mock WebSocket ---------------------------------------------------------

class MockWebSocket {
  static instances = [];
  static CONNECTING = 0;
  static OPEN = 1;

  constructor(url) {
    this.url = url;
    this.readyState = MockWebSocket.CONNECTING;
    this.sent = []; // every client message, parsed
    MockWebSocket.instances.push(this);
  }

  send(data) {
    this.sent.push(JSON.parse(data));
  }

  close() {} // component-initiated; the server side drives onclose in tests

  // Test-side helpers simulating the server end of the socket.
  serverOpen() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  serverSend(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }

  serverClose() {
    this.readyState = 3;
    this.onclose?.();
  }
}

// -- Fixtures ---------------------------------------------------------------

const SESSION = { gameId: "g1234567", token: "tok", seat: 0, name: "Ann" };

const PACK = [
  { id: "c05", name: "Thorn Witch", power: 5 },
  { id: "c07", name: "Ash Phoenix", power: 7 },
  { id: "c09", name: "Dawn Cleric", power: 3 },
  { id: "c11", name: "Rune Smith", power: 5 },
  { id: "c13", name: "Bog Shambler", power: 1 },
];

function snap(overrides = {}) {
  return {
    type: "snapshot",
    game_id: SESSION.gameId,
    status: "active",
    you: { seat: 0, name: "Ann" },
    players: [0, 1, 2, 3].map((s) => ({
      seat: s,
      name: `P${s}`,
      connected: true,
      submitted: s === 0,
    })),
    round: 1,
    rounds_total: 5,
    your_pack: [],
    your_original_pack: PACK,
    your_pick: PACK[1], // Ash Phoenix, currently selected
    your_pick_auto: false,
    your_pick_revision: 2,
    your_picks_all: [],
    revealed: [],
    deadline: Date.now() / 1000 + 30,
    server_now: Date.now() / 1000,
    timeout_seconds: 30,
    ...overrides,
  };
}

function renderGame() {
  sessionStorage.setItem("draft.session", JSON.stringify(SESSION));
  render(<App />);
  const ws = MockWebSocket.instances[0];
  act(() => ws.serverOpen());
  return ws;
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
  sessionStorage.clear();
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// -- Tests ------------------------------------------------------------------

describe("change-pick UI", () => {
  it("断线重连后仍显示原包与当前选择，并可在新连接上改选", () => {
    const ws1 = renderGame();
    act(() => ws1.serverSend(snap()));

    // Change entry: original pack visible, current pick marked and disabled.
    expect(screen.getByText(/已选择 Ash Phoenix/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Ash Phoenix/ }).disabled).toBe(true);
    expect(screen.getByRole("button", { name: /Dawn Cleric/ }).disabled).toBe(false);

    // The connection drops; the hook reconnects by itself (1s retry).
    act(() => ws1.serverClose());
    act(() => {
      vi.advanceTimersByTime(1100);
    });
    expect(MockWebSocket.instances).toHaveLength(2);

    // The server re-sends the personalized snapshot on the new socket:
    // original pack and current pick are still there.
    const ws2 = MockWebSocket.instances[1];
    act(() => ws2.serverOpen());
    act(() => ws2.serverSend(snap()));
    expect(screen.getByText(/已选择 Ash Phoenix/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Ash Phoenix/ }).disabled).toBe(true);

    // And the change entry works on the reconnected socket.
    fireEvent.click(screen.getByRole("button", { name: /Dawn Cleric/ }));
    const req = ws2.sent.find((m) => m.type === "change_pick");
    expect(req).toMatchObject({ card: "c09", round: 1, expected_revision: 2 });
  });

  it("改选请求携带轮次、预期修订与新牌，ack 与快照更新界面", () => {
    const ws = renderGame();
    act(() => ws.serverSend(snap()));

    fireEvent.click(screen.getByRole("button", { name: /Dawn Cleric/ }));
    const req = ws.sent.find((m) => m.type === "change_pick");
    expect(req).toMatchObject({ card: "c09", round: 1, expected_revision: 2 });

    act(() =>
      ws.serverSend({
        type: "change_ack",
        round: 1,
        card: "c09",
        revision: 3,
        client_msg_id: req.client_msg_id,
      })
    );
    act(() => ws.serverSend(snap({ your_pick: PACK[2], your_pick_revision: 3 })));

    expect(screen.getByText(/已选择 Dawn Cleric/)).toBeTruthy();
    expect(screen.getByText(/当前为第 3 版/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Dawn Cleric/ }).disabled).toBe(true);
    expect(screen.getByRole("button", { name: /Ash Phoenix/ }).disabled).toBe(false);
  });

  it("未提交时点击手牌仍发送首次 pick", () => {
    const ws = renderGame();
    act(() =>
      ws.serverSend(
        snap({
          your_pack: PACK,
          your_pick: null,
          your_pick_revision: null,
          players: [0, 1, 2, 3].map((s) => ({
            seat: s,
            name: `P${s}`,
            connected: true,
            submitted: false,
          })),
        })
      )
    );

    fireEvent.click(screen.getByRole("button", { name: /Thorn Witch/ }));
    const req = ws.sent.find((m) => m.type === "pick");
    expect(req).toMatchObject({ card: "c05", round: 1 });
    expect(ws.sent.some((m) => m.type === "change_pick")).toBe(false);
  });

  it("超时自动选择不提供改选入口", () => {
    const ws = renderGame();
    act(() => ws.serverSend(snap({ your_pick_auto: true })));

    expect(screen.getByText(/超时自动/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Dawn Cleric/ })).toBeNull();
    expect(ws.sent.some((m) => m.type === "change_pick")).toBe(false);
  });
});
