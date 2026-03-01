import { describe, it, expect } from "vitest";
import { qualityColor, qualityTextColor, hotColdIcon, hotColdColor } from "../intel-badge";

describe("qualityColor", () => {
  it("returns fallback for null", () => {
    expect(qualityColor(null)).toBe("bg-muted-foreground/30");
  });

  it("returns fallback for undefined", () => {
    expect(qualityColor(undefined)).toBe("bg-muted-foreground/30");
  });

  it("maps elite to primary", () => {
    expect(qualityColor("elite")).toBe("bg-primary");
  });

  it("maps strong to success", () => {
    expect(qualityColor("strong")).toBe("bg-sem-success");
  });

  it("maps average to neutral", () => {
    expect(qualityColor("average")).toBe("bg-sem-neutral");
  });

  it("maps below to warning", () => {
    expect(qualityColor("below")).toBe("bg-sem-warning");
  });

  it("maps poor to risk", () => {
    expect(qualityColor("poor")).toBe("bg-sem-risk");
  });

  it("returns fallback for unknown tier", () => {
    expect(qualityColor("unknown")).toBe("bg-muted-foreground/30");
  });
});

describe("qualityTextColor", () => {
  it("returns fallback for null", () => {
    expect(qualityTextColor(null)).toBe("text-muted-foreground");
  });

  it("returns fallback for undefined", () => {
    expect(qualityTextColor(undefined)).toBe("text-muted-foreground");
  });

  it("maps elite to primary text", () => {
    expect(qualityTextColor("elite")).toBe("text-primary");
  });

  it("maps strong to success text", () => {
    expect(qualityTextColor("strong")).toBe("text-sem-success");
  });

  it("maps average to neutral text", () => {
    expect(qualityTextColor("average")).toBe("text-sem-neutral");
  });

  it("maps below to warning text", () => {
    expect(qualityTextColor("below")).toBe("text-sem-warning");
  });

  it("maps poor to risk text", () => {
    expect(qualityTextColor("poor")).toBe("text-sem-risk");
  });
});

describe("hotColdIcon", () => {
  it("returns empty for null", () => {
    expect(hotColdIcon(null)).toBe("");
  });

  it("returns empty for undefined", () => {
    expect(hotColdIcon(undefined)).toBe("");
  });

  it("returns fire for hot", () => {
    expect(hotColdIcon("hot")).toBe("\u{1F525}");
  });

  it("returns up arrow for warm", () => {
    expect(hotColdIcon("warm")).toBe("\u2191");
  });

  it("returns empty for neutral", () => {
    expect(hotColdIcon("neutral")).toBe("");
  });

  it("returns snowflake for cold", () => {
    expect(hotColdIcon("cold")).toBe("\u2744\uFE0F");
  });

  it("returns double snowflake for ice", () => {
    expect(hotColdIcon("ice")).toBe("\u2744\uFE0F\u2744\uFE0F");
  });
});

describe("hotColdColor", () => {
  it("returns empty for null", () => {
    expect(hotColdColor(null)).toBe("");
  });

  it("returns empty for undefined", () => {
    expect(hotColdColor(undefined)).toBe("");
  });

  it("maps hot to red", () => {
    expect(hotColdColor("hot")).toBe("text-red-500");
  });

  it("maps warm to orange", () => {
    expect(hotColdColor("warm")).toBe("text-orange-400");
  });

  it("maps neutral to muted", () => {
    expect(hotColdColor("neutral")).toBe("text-muted-foreground");
  });

  it("maps cold to blue-400", () => {
    expect(hotColdColor("cold")).toBe("text-blue-400");
  });

  it("maps ice to blue-500", () => {
    expect(hotColdColor("ice")).toBe("text-blue-500");
  });
});
