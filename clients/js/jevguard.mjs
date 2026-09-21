// jevguard JavaScript client: talk to a jevguard server from Node, Deno, Bun, edge runtimes or the browser.
//
//   import { JevGuard } from "./jevguard.mjs";
//   const guard = new JevGuard("http://127.0.0.1:7860", { apiKey: process.env.JEVGUARD_API_KEY });
//   const d = await guard.checkInput(userText, { sessionId: "s-1" });
//   if (d.blocked) return "I can't help with that.";
//
// Also exported: guardTool() to wrap any async tool function, and jevguardMiddleware() for the
// Vercel AI SDK's wrapLanguageModel (input + output checks around generateText).

const STAGES = ["input", "tool_call", "tool_result", "retrieval", "output"];

export class GuardBlockedError extends Error {
  constructor(decision) {
    super(`[jevguard] ${decision.stage} blocked: ${decision.reason}`);
    this.decision = decision;
  }
}

export class JevGuard {
  /**
   * @param {string} baseUrl  jevguard server, e.g. http://127.0.0.1:7860
   * @param {{apiKey?: string, timeoutMs?: number, failClosed?: boolean, agent?: string, framework?: string}} [opts]
   */
  constructor(baseUrl = "http://127.0.0.1:7860", opts = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.apiKey = opts.apiKey;
    this.timeoutMs = opts.timeoutMs ?? 150_000; // escalations may wait on a human
    this.failClosed = opts.failClosed ?? true;
    this.agent = opts.agent;
    this.framework = opts.framework ?? "js";
  }

  /** Low-level call. `context` accepts camelCase or snake_case keys. */
  async check(stage, text = "", context = {}) {
    if (!STAGES.includes(stage)) throw new Error(`unknown stage ${stage}`);
    const ctx = {
      session_id: context.sessionId ?? context.session_id,
      agent: context.agent ?? this.agent,
      framework: context.framework ?? this.framework,
      user_id: context.userId ?? context.user_id,
      tool_name: context.toolName ?? context.tool_name,
      tool_args: context.toolArgs ?? context.tool_args,
      user_goal: context.userGoal ?? context.user_goal,
      grounding: context.grounding,
      metadata: context.metadata,
    };
    Object.keys(ctx).forEach((k) => ctx[k] === undefined && delete ctx[k]);
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const res = await fetch(`${this.baseUrl}/api/guard`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {}) },
        body: JSON.stringify({ stage, text, context: ctx }),
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(`jevguard server returned ${res.status}`);
      return await res.json();
    } catch (err) {
      // Guard unreachable: fail closed by default rather than letting traffic through unchecked.
      return {
        stage, action: this.failClosed ? "block" : "allow", blocked: this.failClosed, risk: this.failClosed ? 1 : 0,
        reason: `guard unavailable (${err.message})`, source: "client_fallback", findings: [], redacted_text: null,
      };
    } finally {
      clearTimeout(timer);
    }
  }

  checkInput(text, ctx) { return this.check("input", text, ctx); }
  checkToolCall(toolName, args = {}, ctx = {}) { return this.check("tool_call", "", { ...ctx, toolName, toolArgs: args }); }
  checkToolResult(text, toolName, ctx = {}) { return this.check("tool_result", text, { ...ctx, toolName }); }
  checkRetrieval(text, ctx) { return this.check("retrieval", text, ctx); }
  checkOutput(text, ctx) { return this.check("output", text, ctx); }
}

/** Wrap an async tool `(args) => result`: check the call first, then its string result. */
export function guardTool(guard, name, fn, { onBlock = "message", ctx = {} } = {}) {
  return async (args, ...rest) => {
    const call = await guard.checkToolCall(name, args, ctx);
    if (call.blocked) {
      if (onBlock === "throw") throw new GuardBlockedError(call);
      return `[tool call blocked by security policy: ${call.reason}]`;
    }
    const result = await fn(args, ...rest);
    if (typeof result !== "string") return result;
    const r = await guard.checkToolResult(result, name, ctx);
    if (r.blocked) {
      if (onBlock === "throw") throw new GuardBlockedError(r);
      return `[tool output withheld by security policy: ${r.reason}]`;
    }
    return r.action === "redact" && r.redacted_text ? r.redacted_text : result;
  };
}

/**
 * Vercel AI SDK middleware:  wrapLanguageModel({ model, middleware: jevguardMiddleware(guard) })
 * Checks the last user message before the call and the generated text after it.
 */
export function jevguardMiddleware(guard, { blockMessage = "I can't help with that request.", ctx = {} } = {}) {
  const lastUserText = (params) => {
    const msgs = params.prompt || [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role !== "user") continue;
      const c = msgs[i].content;
      return typeof c === "string" ? c : (c || []).filter((p) => p.type === "text").map((p) => p.text).join("\n");
    }
    return "";
  };
  const refusal = (d) => ({
    content: [{ type: "text", text: blockMessage }], text: blockMessage, finishReason: "stop",
    usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 }, warnings: [], providerMetadata: { jevguard: d },
  });
  return {
    async wrapGenerate({ doGenerate, params }) {
      const input = lastUserText(params);
      if (input) {
        const d = await guard.checkInput(input, ctx);
        if (d.blocked) return refusal(d);
      }
      const result = await doGenerate();
      const text = result.text ?? (result.content || []).filter((p) => p.type === "text").map((p) => p.text).join("");
      if (text) {
        const d = await guard.checkOutput(text, { ...ctx, userGoal: input });
        if (d.blocked) return { ...result, ...refusal(d) };
      }
      return result;
    },
  };
}
