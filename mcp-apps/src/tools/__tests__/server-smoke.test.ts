import { describe, it, expect } from "vitest";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { createPlainServer, createServer } from "../../../server.js";

describe("createServer", () => {
  it("returns an McpServer instance without throwing", () => {
    const server = createServer();
    expect(server).toBeInstanceOf(McpServer);
  });
});

describe("createPlainServer", () => {
  it("returns an McpServer instance without throwing", () => {
    const server = createPlainServer();
    expect(server).toBeInstanceOf(McpServer);
  });

  it("registers tools without ext-app resource metadata", () => {
    const server = createPlainServer() as unknown as {
      _registeredTools: Record<string, { _meta?: Record<string, unknown> }>;
    };
    const latestOuting = server._registeredTools.mlb_latest_outing;
    const playerStats = server._registeredTools.yahoo_player_stats;
    expect(latestOuting).toBeTruthy();
    expect(playerStats).toBeTruthy();
    expect(latestOuting._meta).toBeUndefined();
    expect(playerStats._meta).toBeUndefined();
  });
});
