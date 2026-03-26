import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiGet, toolError } from "../api/python-client.js";
import {
  str,
  type BestAvailableResponse,
  type IntelPlayerReportResponse,
  type LeagueContextResponse,
  type MlbLatestOutingResponse,
  type MlbPlayerResponse,
  type MlbScheduleResponse,
  type MlbStatsResponse,
  type PlayerStatsResponse,
  type RosterResponse,
} from "../api/types.js";

function textResult(text: string, structuredContent: Record<string, unknown>) {
  return {
    content: [{ type: "text" as const, text }],
    structuredContent,
  };
}

export function registerPlainTools(server: McpServer) {
  server.registerTool(
    "yahoo_league_context",
    {
      description: "Compact league profile: waiver type, scoring format, stat categories, roster slots, and FAAB balance if applicable.",
      annotations: { readOnlyHint: true },
    },
    async () => {
      try {
        const data = await apiGet<LeagueContextResponse>("/api/league-context");
        const lines: string[] = ["LEAGUE CONTEXT:"];
        lines.push("  Waiver: " + str(data.waiver_type) + (data.faab_balance != null ? " ($" + data.faab_balance + " remaining)" : ""));
        lines.push("  Scoring: " + str(data.scoring_type));
        lines.push("  Teams: " + str(data.num_teams) + " | Max adds/week: " + str(data.max_weekly_adds));
        const batCats = (data.stat_categories || []).filter((c) => c.position_type === "B").map((c) => str(c.name));
        const pitCats = (data.stat_categories || []).filter((c) => c.position_type === "P").map((c) => str(c.name));
        if (batCats.length > 0) lines.push("  Bat cats: " + batCats.join(", "));
        if (pitCats.length > 0) lines.push("  Pit cats: " + pitCats.join(", "));
        return textResult(lines.join("\n"), { type: "league-context", ai_recommendation: null, ...data });
      } catch (e) { return toolError(e); }
    },
  );

  server.registerTool(
    "yahoo_roster",
    {
      description: "Show current fantasy baseball roster with positions and eligibility.",
      annotations: { readOnlyHint: true },
    },
    async () => {
      try {
        const data = await apiGet<RosterResponse>("/api/roster");
        const text = "Current Roster:\n" + (data.players || []).map((p) =>
          "  " + str(p.position || "?").padEnd(4) + " " + str(p.name).padEnd(25) + " " + ((p.eligible_positions || []).join(",") || "?")
            + (p.status ? " [" + p.status + "]" : "")
        ).join("\n");
        return textResult(text, { type: "roster", ai_recommendation: null, ...data });
      } catch (e) { return toolError(e); }
    },
  );

  server.registerTool(
    "yahoo_player_stats",
    {
      description: "Get a player's fantasy stats from Yahoo. period: season, average_season, lastweek, lastmonth, week, or date.",
      inputSchema: {
        player_name: z.string().describe("Player name to look up"),
        period: z.string().describe("Stats period: season, average_season, lastweek, lastmonth, week, date").default("season"),
        week: z.string().describe("Week number when period=week").default(""),
        date: z.string().describe("Date YYYY-MM-DD when period=date").default(""),
      },
      annotations: { readOnlyHint: true },
    },
    async ({ player_name, period, week, date }) => {
      try {
        const params: Record<string, string> = { name: player_name, period };
        if (week) params.week = week;
        if (date) params.date = date;
        const data = await apiGet<PlayerStatsResponse>("/api/player-stats", params);
        const lines = ["Stats for " + data.player_name + " (" + data.period + "):"];
        for (const [key, val] of Object.entries(data.stats || {})) {
          if (key !== "player_id" && key !== "name") lines.push("  " + str(key).padEnd(20) + str(val));
        }
        return textResult(lines.join("\n"), { type: "player-stats", ai_recommendation: null, ...data });
      } catch (e) { return toolError(e); }
    },
  );

  server.registerTool(
    "yahoo_best_available",
    {
      description: "Show best available players ranked by z-score. pos_type: B for batters, P for pitchers.",
      inputSchema: {
        pos_type: z.string().describe("B for batters, P for pitchers").default("B"),
        count: z.number().describe("Number of players to return").default(25),
      },
      annotations: { readOnlyHint: true },
    },
    async ({ pos_type, count }) => {
      try {
        const data = await apiGet<BestAvailableResponse>("/api/best-available", { pos_type, count: String(count), include_intel: "false" });
        const label = pos_type === "B" ? "Hitters" : "Pitchers";
        const text = "Best Available " + label + ":\n" + (data.players || []).map((p) =>
          "  " + String(p.rank).padStart(3) + ". " + str(p.name).padEnd(25) + " " + str((p.positions || []).join(",")).padEnd(12) + " z=" + (p.z_score != null ? p.z_score.toFixed(2) : "N/A")
        ).join("\n");
        return textResult(text, { type: "best-available", ai_recommendation: null, ...data });
      } catch (e) { return toolError(e); }
    },
  );

  server.registerTool(
    "yahoo_browser_status",
    {
      description: "Check if the browser session for write operations is valid. If not valid, user needs to run './yf browser-login'.",
      annotations: { readOnlyHint: true },
    },
    async () => {
      try {
        const data = await apiGet<{ valid: boolean; reason?: string; cookie_count?: number }>("/api/browser-login-status");
        const text = data.valid
          ? "Browser session is valid (" + (data.cookie_count || 0) + " Yahoo cookies)"
          : "Browser session not valid: " + (data.reason || "unknown") + ". Run './yf browser-login' to set up.";
        return textResult(text, { type: "browser-status", ai_recommendation: null, ...data });
      } catch (e) { return toolError(e); }
    },
  );

  server.registerTool(
    "fantasy_player_report",
    {
      description: "Deep-dive Statcast + trends + plate discipline + Reddit buzz for a single player.",
      inputSchema: { player_name: z.string().describe("Player name to look up") },
      annotations: { readOnlyHint: true },
    },
    async ({ player_name }) => {
      try {
        const data = await apiGet<IntelPlayerReportResponse>("/api/intel/player", { name: player_name });
        const lines = ["Player Intelligence: " + str(data.name)];
        if (data.statcast) {
          lines.push("  Tier: " + str(data.statcast.quality_tier || "unknown"));
          if (data.statcast.xwoba != null) lines.push("  xwOBA: " + data.statcast.xwoba);
          if (data.statcast.avg_exit_velo != null) lines.push("  Exit Velo: " + data.statcast.avg_exit_velo);
        }
        if (data.trends?.hot_cold) lines.push("  Trend: " + data.trends.hot_cold);
        return textResult(lines.join("\n"), { type: "intel-player", ai_recommendation: null, ...data });
      } catch (e) { return toolError(e); }
    },
  );

  server.registerTool(
    "mlb_player",
    {
      description: "Get MLB player info by MLB Stats API player ID.",
      inputSchema: { player_id: z.string().describe("MLB Stats API player ID") },
      annotations: { readOnlyHint: true },
    },
    async ({ player_id }) => {
      try {
        const data = await apiGet<MlbPlayerResponse>("/api/mlb/player", { player_id });
        const text = "Player: " + data.name + "\n"
          + "  Position: " + data.position + "\n"
          + "  Team: " + data.team + "\n"
          + "  Bats/Throws: " + data.bats + "/" + data.throws + "\n"
          + "  Age: " + data.age + "\n"
          + "  MLB ID: " + data.mlb_id;
        return textResult(text, { type: "mlb-player", ai_recommendation: null, ...data });
      } catch (e) { return toolError(e); }
    },
  );

  server.registerTool(
    "mlb_stats",
    {
      description: "Get player season stats by MLB Stats API player ID.",
      inputSchema: {
        player_id: z.string().describe("MLB Stats API player ID"),
        season: z.string().describe("Season year (e.g. 2026)").default("2026"),
      },
      annotations: { readOnlyHint: true },
    },
    async ({ player_id, season }) => {
      try {
        const data = await apiGet<MlbStatsResponse>("/api/mlb/stats", { player_id, season });
        const lines = ["Stats for " + season + ":"];
        for (const [key, val] of Object.entries(data.stats || {})) lines.push("  " + key + ": " + String(val));
        return textResult(lines.join("\n"), { type: "mlb-stats", ai_recommendation: null, ...data });
      } catch (e) { return toolError(e); }
    },
  );

  server.registerTool(
    "mlb_latest_outing",
    {
      description: "Get a player's latest MLB outing or game performance by player name or MLB player ID.",
      inputSchema: {
        player_name: z.string().describe("Player name to look up").default(""),
        player_id: z.string().describe("Optional MLB Stats API player ID").default(""),
        date: z.string().describe("Optional target date in YYYY-MM-DD.").default(""),
      },
      annotations: { readOnlyHint: true },
    },
    async ({ player_name, player_id, date }) => {
      try {
        const params: Record<string, string> = {};
        if (player_name) params.player_name = player_name;
        if (player_id) params.player_id = player_id;
        if (date) params.date = date;
        const data = await apiGet<MlbLatestOutingResponse>("/api/mlb/latest-outing", params);
        const lines = [
          "Latest outing for " + data.player_name + ":",
          "  " + str(data.outing?.date || "?") + " vs " + str(data.outing?.opponent || "?"),
          "  " + data.summary,
        ];
        return textResult(lines.join("\n"), { type: "mlb-latest-outing", ai_recommendation: null, ...data });
      } catch (e) { return toolError(e); }
    },
  );

  server.registerTool(
    "mlb_schedule",
    {
      description: "Show MLB game schedule. Leave date empty for today, or pass YYYY-MM-DD.",
      inputSchema: { date: z.string().describe("Date in YYYY-MM-DD format, empty for today").default("") },
      annotations: { readOnlyHint: true },
    },
    async ({ date }) => {
      try {
        const data = await apiGet<MlbScheduleResponse>("/api/mlb/schedule", date ? { date } : undefined);
        const text = "MLB Schedule for " + data.date + ":\n" + (data.games || []).map((g) =>
          "  " + str(g.away) + " @ " + str(g.home) + " - " + str(g.status)
        ).join("\n");
        return textResult(text, { type: "mlb-schedule", ai_recommendation: null, ...data });
      } catch (e) { return toolError(e); }
    },
  );
}
