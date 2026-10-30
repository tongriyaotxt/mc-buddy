package adris.altoclef.mcp;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.function.Function;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import adris.altoclef.Debug;
import adris.altoclef.Py4jEntryPoint;

/**
 * MCP server (Model Context Protocol over Streamable HTTP) hosted directly by
 * the mod. A cognitive agent (Claude) connects over the LAN
 * (http://&lt;lan-ip&gt;:port/mcp) and drives the bot through the SAME
 * Py4jEntryPoint levers — single source of truth, no py4j hop, no docker-exec.
 *
 * Transport: JSON-RPC 2.0 over a single POST endpoint (/mcp). Requests get an
 * application/json response; notifications get 202. Implements initialize,
 * tools/list, tools/call, ping. Tools are a curated set of agent levers with
 * descriptions + JSON schemas (see docs/features/AGENT_PY4J_LEVERS.md).
 *
 * The HTTP handler runs off-thread; each lever internally marshals to the
 * client thread as needed (onClientThread), so calling them here is safe.
 */
public class McpServer {

    private static final Gson GSON = new Gson();
    private static final String PROTOCOL_VERSION = "2025-06-18";
    // MCP 要求"服务端声明它支持的版本、按客户端的来"——以前写死 2024-11-05，
    // Agent 用 2025-06-18 连的时候会以为服务端是老版本。
    private static final java.util.Set<String> SUPPORTED_PROTOCOLS =
            new java.util.HashSet<>(java.util.Arrays.asList(
                    "2024-11-05", "2025-03-26", "2025-06-18"));

    private final Py4jEntryPoint api;
    private final String authToken;
    private final List<Tool> tools = new ArrayList<>();
    private HttpServer http;

    private static final class Tool {
        final String name, description;
        final JsonObject inputSchema;
        final Function<JsonObject, Object> handler;
        Tool(String n, String d, JsonObject s, Function<JsonObject, Object> h) {
            name = n; description = d; inputSchema = s; handler = h;
        }
    }

    /**
     * @param authToken required as "Authorization: Bearer &lt;authToken&gt;" on every request
     *                  (TODOS.md C7.3 — this server binds 0.0.0.0 with no other access control).
     *                  Must be non-empty: an empty token would accept every request unchecked.
     */
    public McpServer(Py4jEntryPoint api, String authToken) {
        if (authToken == null || authToken.isEmpty()) {
            throw new IllegalArgumentException("McpServer requires a non-empty authToken");
        }
        this.api = api;
        this.authToken = authToken;
        registerTools();
    }

    public void start(int port) throws IOException {
        http = HttpServer.create(new InetSocketAddress("0.0.0.0", port), 0);
        http.createContext("/mcp", this::handle);
        http.createContext("/", this::handle); // tolerate clients that POST to root
        http.setExecutor(Executors.newCachedThreadPool());
        http.start();
    }

    public void stop() {
        if (http != null) { http.stop(0); http = null; }
    }

    // ---- HTTP ----

    private void handle(HttpExchange ex) {
        try {
            ex.getResponseHeaders().add("Access-Control-Allow-Origin", "*");
            ex.getResponseHeaders().add("Access-Control-Allow-Headers", "*");
            ex.getResponseHeaders().add("Access-Control-Allow-Methods", "POST, GET, OPTIONS");
            String method = ex.getRequestMethod();
            if ("OPTIONS".equals(method)) { ex.sendResponseHeaders(204, -1); return; }
            if (!isAuthorized(ex)) { ex.sendResponseHeaders(401, -1); return; }
            if ("GET".equals(method)) { ex.sendResponseHeaders(405, -1); return; } // no server->client push
            if (!"POST".equals(method)) { ex.sendResponseHeaders(405, -1); return; }

            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            JsonElement parsed = JsonParser.parseString(body);

            // 2026-10-27: 长活走 SSE 流式（有 progressToken 且客户端接受 event-stream 才算）
            String accept = ex.getRequestHeaders().getFirst("Accept");
            if (parsed.isJsonObject()) {
                JsonObject req0 = parsed.getAsJsonObject();
                String m0 = req0.has("method") ? req0.get("method").getAsString() : "";
                boolean wantsStream = accept != null && accept.contains("text/event-stream");
                boolean hasToken = req0.has("params") && req0.get("params").isJsonObject()
                        && req0.getAsJsonObject("params").has("_meta")
                        && req0.getAsJsonObject("params").getAsJsonObject("_meta")
                                .has("progressToken");
                if ("tools/call".equals(m0) && wantsStream && hasToken) {
                    handleStreamingCall(ex, req0);
                    return;                      // handleStreamingCall 自己关流
                }
            }

            JsonElement respElem;
            if (parsed.isJsonArray()) {
                JsonArray out = new JsonArray();
                for (JsonElement e : parsed.getAsJsonArray()) {
                    JsonObject r = handleRpc(e.getAsJsonObject());
                    if (r != null) out.add(r);
                }
                respElem = out.size() == 0 ? null : out;
            } else {
                respElem = handleRpc(parsed.getAsJsonObject());
            }

            if (respElem == null) { ex.sendResponseHeaders(202, -1); return; } // notification(s)
            byte[] data = GSON.toJson(respElem).getBytes(StandardCharsets.UTF_8);
            ex.getResponseHeaders().set("Content-Type", "application/json");
            ex.sendResponseHeaders(200, data.length);
            ex.getResponseBody().write(data);
        } catch (Exception e) {
            Debug.logInternal("MCP handler error: " + e);
            try { ex.sendResponseHeaders(500, -1); } catch (IOException ignored) {}
        } finally {
            ex.close();
        }
    }


    // ══════════════════════════════════════════════════════════════════════════
    //  2026-10-27 · MCP progress over SSE
    //  客户端带 `_meta.progressToken` + `Accept: text/event-stream` 时，工具在后台线程跑，
    //  我们每 400ms 发一条 notifications/progress（message = 它自己的任务链）。
    //  跑完再把 JSON-RPC 结果作为最后一条 data: 发出去。
    // ══════════════════════════════════════════════════════════════════════════

    private static void sse(HttpExchange ex, String json) throws IOException {
        ex.getResponseBody().write(("data: " + json + "\n\n").getBytes(StandardCharsets.UTF_8));
        ex.getResponseBody().flush();   // 不 flush 就会被缓冲到响应结束，客户端看不到进度
    }

    private void handleStreamingCall(HttpExchange ex, JsonObject req) throws IOException {
        JsonElement id = req.get("id");
        JsonObject params = req.has("params") ? req.getAsJsonObject("params") : new JsonObject();
        String token = "0";
        if (params.has("_meta") && params.get("_meta").isJsonObject()) {
            JsonObject meta = params.getAsJsonObject("_meta");
            if (meta.has("progressToken")) token = meta.get("progressToken").getAsString();
        }
        ex.getResponseHeaders().set("Content-Type", "text/event-stream");
        ex.getResponseHeaders().set("Cache-Control", "no-cache");
        ex.getResponseHeaders().set("Connection", "keep-alive");
        ex.sendResponseHeaders(200, 0);              // 0 = chunked，长度未知

        // 先发一条 0% 的，保证"秒回的工具"也能证明服务端支持进度
        JsonObject first = progressNote(token, 0, "started");
        sse(ex, GSON.toJson(first));

        java.util.concurrent.CompletableFuture<JsonObject> fut =
                java.util.concurrent.CompletableFuture.supplyAsync(() -> toolsCallResult(params));
        int step = 1;
        long t0 = System.currentTimeMillis();
        while (!fut.isDone()) {
            try {
                Thread.sleep(400);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
                break;
            }
            String what = "";
            try {
                what = api.userTaskName();
                if (what == null || what.isEmpty()) what = api.getTaskChainString();
            } catch (Exception ignored) {
            }
            if (what == null) what = "";
            // 任务链为空时 getTaskChainString() 会回一段中文说明，别把它当"进度"发给客户端
            if (what.startsWith("Description of current game pipeline")) what = "";
            if (what.length() > 160) what = what.substring(0, 160);
            JsonObject note = progressNote(token, step++,
                    what.isEmpty() ? "working" : what.trim());
            try {
                sse(ex, GSON.toJson(note));
            } catch (IOException broken) {
                break;                               // 客户端走了就算了（工具继续跑完）
            }
            if (System.currentTimeMillis() - t0 > 300_000L) {   // 5 分钟硬上限
                break;
            }
        }
        JsonObject result;
        try {
            result = fut.get(5, java.util.concurrent.TimeUnit.SECONDS);
        } catch (Exception e) {
            result = contentResult("progress stream error: " + e.getMessage(), true);
        }
        JsonObject resp = new JsonObject();
        resp.addProperty("jsonrpc", "2.0");
        if (id != null) resp.add("id", id);
        resp.add("result", result);
        sse(ex, GSON.toJson(resp));
        ex.close();
    }

    private static JsonObject progressNote(String token, int progress, String message) {
        JsonObject params = new JsonObject();
        params.addProperty("progressToken", token);
        params.addProperty("progress", progress);
        params.addProperty("message", message);
        JsonObject note = new JsonObject();
        note.addProperty("jsonrpc", "2.0");
        note.addProperty("method", "notifications/progress");
        note.add("params", params);
        return note;
    }

    private boolean isAuthorized(HttpExchange ex) {
        String header = ex.getRequestHeaders().getFirst("Authorization");
        if (header == null || !header.startsWith("Bearer ")) return false;
        String presented = header.substring("Bearer ".length());
        // Constant-time compare -- this is a bearer secret over an unencrypted LAN socket,
        // no reason to leak how many leading characters matched via response timing.
        return java.security.MessageDigest.isEqual(
                presented.getBytes(StandardCharsets.UTF_8), authToken.getBytes(StandardCharsets.UTF_8));
    }

    // ---- JSON-RPC ----

    private JsonObject handleRpc(JsonObject req) {
        JsonElement id = req.get("id");
        if (id == null || id.isJsonNull()) return null; // notification — no reply
        String method = req.has("method") ? req.get("method").getAsString() : "";
        try {
            JsonObject result;
            switch (method) {
                case "initialize":   result = initializeResult(req.getAsJsonObject("params")); break;
                case "ping":         result = new JsonObject(); break;
                case "tools/list":   result = toolsListResult(); break;
                case "tools/call":   result = toolsCallResult(req.getAsJsonObject("params")); break;
                case "resources/list": result = resourcesListResult(); break;
                case "resources/read": result = resourcesReadResult(req.getAsJsonObject("params")); break;
                case "prompts/list":   result = promptsListResult(); break;
                default:             return error(id, -32601, "Method not found: " + method);
            }
            JsonObject resp = new JsonObject();
            resp.addProperty("jsonrpc", "2.0");
            resp.add("id", id);
            resp.add("result", result);
            return resp;
        } catch (Exception e) {
            return error(id, -32603, "Internal error: " + e.getMessage());
        }
    }

    private JsonObject initializeResult(JsonObject params) {
        JsonObject caps = new JsonObject();
        caps.add("tools", new JsonObject());
        // 只读数据走 resources（MCP 的正当姿势）：state / inventory / chat / entities / taskchain
        JsonObject res = new JsonObject();
        res.addProperty("subscribe", false);
        res.addProperty("listChanged", false);
        caps.add("resources", res);
        caps.add("prompts", new JsonObject());
        JsonObject info = new JsonObject();
        info.addProperty("name", "buddy");
        info.addProperty("version", "1.1.0");
        String want = params != null && params.has("protocolVersion")
                ? params.get("protocolVersion").getAsString() : PROTOCOL_VERSION;
        JsonObject r = new JsonObject();
        r.addProperty("protocolVersion",
                SUPPORTED_PROTOCOLS.contains(want) ? want : PROTOCOL_VERSION);
        r.add("capabilities", caps);
        r.add("serverInfo", info);
        r.addProperty("instructions",
                "Levers to drive a Minecraft bot as a cognitive agent. Loop: getGameState (see) "
                + "-> gotoXYZ/pathStatus (move) -> combat/build/menu (act). You decide strategy; "
                + "the mod executes mechanics.");
        return r;
    }

    private JsonObject toolsListResult() {
        JsonArray arr = new JsonArray();
        for (Tool t : tools) {
            JsonObject to = new JsonObject();
            to.addProperty("name", t.name);
            to.addProperty("title", t.name);
            String desc = t.description;
            String prefer = ALIAS_OF.get(t.name);
            if (prefer != null) {
                desc = "[overlaps with " + prefer + " -- pick one deliberately] " + desc;
            }
            to.addProperty("description", desc);
            to.add("inputSchema", t.inputSchema);
            to.add("annotations", annotationsFor(t.name));
            arr.add(to);
        }
        JsonObject r = new JsonObject();
        r.add("tools", arr);
        return r;
    }

    private JsonObject toolsCallResult(JsonObject params) {
        String name = params.get("name").getAsString();
        JsonObject args = params.has("arguments") && params.get("arguments").isJsonObject()
                ? params.getAsJsonObject("arguments") : new JsonObject();
        for (Tool t : tools) {
            if (t.name.equals(name)) {
                boolean isError = false;
                String text;
                try {
                    Object result = t.handler.apply(args);
                    // 2026-10-27：lever 很多已经返回 JSON 字符串，GSON.toJson(String) 会再包一层引号
                    // ⇒ 客户端要连解两次（我们 Python 侧 as_obj() 解 4 层就是被这个逼出来的）。
                    if (result instanceof String) {
                        String sr = ((String) result).trim();
                        text = (sr.startsWith("{") || sr.startsWith("[")) ? sr : GSON.toJson(result);
                    } else {
                        text = GSON.toJson(result);
                    }
                } catch (Exception e) {
                    isError = true;
                    text = "Tool error: " + e.getMessage();
                }
                return contentResult(text, isError);
            }
        }
        return contentResult("Unknown tool: " + name, true);
    }

    private JsonObject contentResult(String text, boolean isError) {
        JsonObject content = new JsonObject();
        content.addProperty("type", "text");
        content.addProperty("text", text);
        JsonArray arr = new JsonArray();
        arr.add(content);
        JsonObject r = new JsonObject();
        r.add("content", arr);
        r.addProperty("isError", isError);
        return r;
    }

    private JsonObject error(JsonElement id, int code, String msg) {
        JsonObject err = new JsonObject();
        err.addProperty("code", code);
        err.addProperty("message", msg);
        JsonObject resp = new JsonObject();
        resp.addProperty("jsonrpc", "2.0");
        resp.add("id", id);
        resp.add("error", err);
        return resp;
    }


    // ---- MCP annotations（模型据此判断"这个调用会不会改世界"） ----
    private static final java.util.Set<String> READ_ONLY_TOOLS = new java.util.HashSet<>(
            java.util.Arrays.asList(
                    "getGameState", "getBlockAt", "getBlocksAround", "getSurfaceMap", "getEntities",
                    "getOpenScreen", "getRecentChat", "inGame", "playerDetail", "pathStatus",
                    "mineStatus", "taskChain", "taskChainText", "scanBlocks", "canReach",
                    "canBreakBlock", "canPlaceBlock", "inventorySpace", "selectionSize",
                    "undoStatus", "replaceStatus", "runAwayStatus", "punkStatus", "voiceStatus", "listCommands"));

    private static final java.util.Set<String> DESTRUCTIVE_TOOLS = new java.util.HashSet<>(
            java.util.Arrays.asList(
                    "destroyBlockAt", "fillSelection", "replaceSelection", "clearProtectedAreas",
                    "buildQueueClear", "cleanupScaffold", "mineBlock", "attackPlayer", "punk",
                    "punkAny", "respawnPlayer", "ConnectToServer", "pickupFromContainer"));

    // 2026-10-27: 有几对工具干的是同一件事（mineBlock / destroyBlockAt / placeBlockAt / buildBlocks）。
    // 模型靠 description 选工具，所以在**别名**的描述前面点明"优先用哪个"，别再让它猜。
    private static final java.util.Map<String, String> ALIAS_OF = new java.util.HashMap<>();
    static {
        ALIAS_OF.put("destroyBlockAt", "mineBlock");
        ALIAS_OF.put("placeBlockAt", "placeBlockLooking");
        ALIAS_OF.put("buildBlocks", "placeBlockLooking (single block) / buildQueue (many)");
        ALIAS_OF.put("mineStatus", "pathStatus + mineStatus are different things: mine = break queue");
    }

    private static JsonObject annotationsFor(String name) {
        JsonObject a = new JsonObject();
        boolean ro = READ_ONLY_TOOLS.contains(name);
        boolean de = DESTRUCTIVE_TOOLS.contains(name);
        a.addProperty("readOnlyHint", ro);
        a.addProperty("destructiveHint", de);
        a.addProperty("idempotentHint", ro);
        a.addProperty("openWorldHint", true);
        return a;
    }

    // ---- resources：只读数据（MCP 的正当姿势；模型读它，比多造 5 个工具省 context） ----
    private JsonObject resource(String uri, String name, String desc, String mime) {
        JsonObject o = new JsonObject();
        o.addProperty("uri", uri);
        o.addProperty("name", name);
        o.addProperty("description", desc);
        o.addProperty("mimeType", mime);
        return o;
    }

    private JsonObject resourcesListResult() {
        JsonArray arr = new JsonArray();
        arr.add(resource("minecraft://state", "game-state",
                "Bot + players + nearby beds snapshot (same data as getGameState)", "application/json"));
        arr.add(resource("minecraft://inventory", "inventory",
                "Free slots + item counts (same data as inventorySpace)", "application/json"));
        arr.add(resource("minecraft://chat/recent", "recent-chat",
                "Last 40 chat lines (event/error markers)", "application/json"));
        arr.add(resource("minecraft://entities/nearby", "nearby-entities",
                "Non-player entities around the bot with distance/position", "application/json"));
        arr.add(resource("minecraft://taskchain", "task-chain",
                "Current AltoClef task chain (empty = idle)", "text/plain"));
        JsonObject r = new JsonObject();
        r.add("resources", arr);
        return r;
    }

    private JsonObject resourcesReadResult(JsonObject params) {
        String uri = params != null && params.has("uri") ? params.get("uri").getAsString() : "";
        String text;
        String mime = "application/json";
        try {
            switch (uri) {
                case "minecraft://state":      text = raw(api.getGameState()); break;
                case "minecraft://inventory":  text = raw(api.inventorySpace()); break;
                case "minecraft://chat/recent": text = raw(api.getRecentChat(40)); break;
                case "minecraft://entities/nearby": text = raw(api.getEntitiesInfo(24)); break;
                case "minecraft://taskchain":
                    text = raw(api.getTaskChainString());
                    mime = "text/plain";
                    break;
                default:
                    return error(JsonParser.parseString("0"), -32602, "Unknown resource: " + uri);
            }
        } catch (Exception e) {
            text = "{\"error\":\"" + e.getMessage() + "\"}";
        }
        JsonObject c = new JsonObject();
        c.addProperty("uri", uri);
        c.addProperty("mimeType", mime);
        c.addProperty("text", text);
        JsonArray arr = new JsonArray();
        arr.add(c);
        JsonObject r = new JsonObject();
        r.add("contents", arr);
        return r;
    }

    private static String raw(Object o) {
        if (o instanceof String) return ((String) o);
        return GSON.toJson(o);
    }

    private JsonObject promptsListResult() {
        JsonArray arr = new JsonArray();
        String[][] ps = {
                {"goto_and_see", "走到某个坐标并报告路上看到了什么"},
                {"bring_item", "去弄到 N 个某物品并交到玩家手里"},
                {"guard_player", "守在玩家身边，清掉靠近的敌对生物"},
        };
        for (String[] p : ps) {
            JsonObject o = new JsonObject();
            o.addProperty("name", p[0]);
            o.addProperty("description", p[1]);
            JsonArray args = new JsonArray();
            JsonObject a1 = new JsonObject();
            a1.addProperty("name", "goal");
            a1.addProperty("description", "自然语言目标");
            a1.addProperty("required", true);
            args.add(a1);
            o.add("arguments", args);
            arr.add(o);
        }
        JsonObject r = new JsonObject();
        r.add("prompts", arr);
        return r;
    }

    // ---- 工具注册 ----

    private void tool(String name, String desc, JsonObject schema, Function<JsonObject, Object> h) {
        tools.add(new Tool(name, desc, schema, h));
    }

    private void registerTools() {
        // perception
        tool("getGameState",
                "Battle perception (the agent's eyes): self(hp/maxHp/armor/pos/onGround/held/blocks), "
                + "players[](name/pos/distance/hp/sprinting), beds[](nearby beds). Read before deciding tactics.",
                schema(), a -> api.getGameState());
        tool("inventorySpace",
                "Free inventory slots + block counts by type (resource planning before bridging/building).",
                schema(), a -> api.inventorySpace());
        tool("getOpenScreen",
                "Read the open menu (title + every slot: item id / display name / count) — shop or chest.",
                schema(), a -> api.getOpenScreen());
        tool("getRecentChat",
                "Last n chat messages (event/error markers).",
                schema("n:int"), a -> api.getRecentChat(argInt(a, "n")));
        tool("canReach",
                "Heuristic: can we path to a cell (optionally breaking blocks)? Returns reached/pathSize/breaks/endDistance.",
                schema("x:int", "y:int", "z:int", "withBreaking:bool"),
                a -> api.canReach(argInt(a, "x"), argInt(a, "y"), argInt(a, "z"), argBool(a, "withBreaking")));
        tool("getBlockAt",
                "Read the actual block at a world coordinate (id, name, is_air) straight from the client's "
                + "world -- ground truth for diagnosing a stuck route without guessing terrain from log lines.",
                schema("x:int", "y:int", "z:int"),
                a -> api.getBlockAt(argInt(a, "x"), argInt(a, "y"), argInt(a, "z")));
        tool("getBlocksAround",
                "Every non-air block in a cube of the given radius around the bot (position/block/hardness),"
                + " radius capped at 5. A local terrain mini-map -- read it before guessing why a route stalled.",
                schema("radius:int"), a -> api.getBlocksAround(argInt(a, "radius")));
        tool("getSurfaceMap",
                "Top-down surface map around the bot: for each (dx,dz) column within radius, the highest"
                + " non-air block's height and hardness relative to the bot's feet. Radius capped at 6.",
                schema("radius:int"), a -> api.getSurfaceMap(argInt(a, "radius")));

        // movement
        tool("gotoXYZ",
                "Navigate to a world coordinate via the tungsten pathfinder (walk/parkour/bridge). "
                + "Fire-and-poll: then call pathStatus repeatedly until arrived.",
                schema("x:int", "y:int", "z:int"),
                a -> api.gotoXYZ(argInt(a, "x"), argInt(a, "y"), argInt(a, "z")));
        tool("pathStatus",
                "Poll the current navigation: busy/pos/distance-to-goal/arrived(<1.5). Loop after gotoXYZ until arrived.",
                schema(), a -> api.pathStatus());
        tool("gotoFar",
                "Navigate toward a FAR target with a receding horizon (huge goals freeze the pathfinder). "
                + "Advances one segment (<= horizon blocks) toward the target. Loop: gotoFar -> pathStatus "
                + "until arrived -> gotoFar, until finalSegment=true.",
                schema("x:int", "y:int", "z:int", "horizon:int"),
                a -> api.gotoFar(argInt(a, "x"), argInt(a, "y"), argInt(a, "z"), argInt(a, "horizon")));
        tool("stopPathing",
                "Stop all navigation and tasks (tungsten ;stop + altoclef @stop).",
                schema(), a -> api.stopPathing());
        tool("setTungstenPathing",
                "Toggle routing altoclef/shredder navigation through tungsten's physics (useTungsten + "
                + "experimentalPathfinding). On = @goto/@get/@gamer delegate qualifying segments to tungsten.",
                schema("on:bool"), a -> api.setTungstenPathing(argBool(a, "on")));
        tool("pathingMode", "Read the current pathing-delegation flags.", schema(), a -> api.pathingMode());
        tool("bridgeTo",
                "Godbridge toward a target coordinate (continuous pave-ahead). Needs a block in hand (selectHotbar first).",
                schema("x:int", "y:int", "z:int"),
                a -> booleanMap(api.bridgeTo(argInt(a, "x"), argInt(a, "y"), argInt(a, "z"))));

        // combat
        tool("mouseClick",
                "Click the mouse: 'left' (attack), 'right' (use), 'middle'. Anti-cheat-safe input pipeline.",
                schema("button:string"), a -> api.mouseClick(argStr(a, "button")));
        tool("shootArrowAt",
                "Shoot a bow at a player with computed ballistic trajectory + target lead.",
                schema("player:string"), a -> booleanMap(api.shootArrowAt(argStr(a, "player"))));
        tool("shieldBlock",
                "Raise the shield for N ticks (blocks arrows/hits).",
                schema("ticks:int"), a -> booleanMap(api.shieldBlock(argInt(a, "ticks"))));
        tool("punk",
                "Hunt one player by name via the tungsten combat engine (A* approach + melee aura).",
                schema("name:string"), a -> api.punk(argStr(a, "name")));
        tool("punkAny",
                "Multi-target hunt: attack the NEAREST player in `allow` (empty = any player), never hitting "
                + "anyone in `avoid`. Auto re-targets. You pick who to fight; the mod executes.",
                schema("allow:strarray", "avoid:strarray"),
                a -> api.punkAny(strList(a, "allow"), strList(a, "avoid")));
        tool("punkAvoid",
                "Update the avoid-list mid-fight (never hit these players).",
                schema("avoid:strarray"), a -> api.punkAvoid(strList(a, "avoid")));
        tool("punkStop", "Stop the tungsten combat engine.", schema(), a -> api.punkStop());
        tool("punkStatus", "Combat status: active + the player currently being fought (null if none).",
                schema(), a -> api.punkStatus());
        tool("runAwayPlayer",
                "Flee a player, keeping at least `distance` blocks (min 3). Mirror of punk — tungsten paths to "
                + "the safest reachable point AWAY from the threat and never backs off into the void. Stops any punk.",
                schema("name:string", "distance:int"),
                a -> api.runAwayPlayer(argStr(a, "name"), argInt(a, "distance")));
        tool("runAwayStop", "Stop fleeing.", schema(), a -> api.runAwayStop());
        tool("runAwayStatus", "Flee status: active + the threat currently tracked (null if none).",
                schema(), a -> api.runAwayStatus());

        // building + WorldEdit
        tool("placeBlockAt",
                "Place a block at a cell (aims at a supporting face, interactBlock). Must be within reach; equip a block first. "
                + "Judged by the WORLD, not by the click: `placed` says whether the cell actually stopped being air. "
                + "A failed call says WHICH failure it was — `cooldown` = the 4-tick place-rate gate (retry in ~200 ms); "
                + "`body_blocked` = your own hitbox covers that cell (step about half a block off its edge and the same "
                + "cell works; retrying from where you stand never will); `permanent` = this client declined the click "
                + "itself, e.g. the face or the cell changed under you.",
                schema("x:int", "y:int", "z:int"),
                a -> api.placeBlockAt(argInt(a, "x"), argInt(a, "y"), argInt(a, "z")));
        tool("select",
                "Set a WorldEdit region (inclusive corners, any order). Shows a yellow selection box.",
                schema("x1:int", "y1:int", "z1:int", "x2:int", "y2:int", "z2:int"),
                a -> api.select(argInt(a, "x1"), argInt(a, "y1"), argInt(a, "z1"),
                                argInt(a, "x2"), argInt(a, "y2"), argInt(a, "z2")));
        tool("clearSelection", "Clear the WorldEdit selection.", schema(), a -> api.clearSelection());
        tool("fillSelection",
                "//set — queue the selection to be filled with a block (bottom-up). Placement is a TICK-DRIVEN "
                + "QUEUE at the human rate (one block per 4 ticks), so this returns immediately with queued=N: "
                + "poll buildQueue() for progress. Equips the named block from the hotbar per cell.",
                schema("block:string"), a -> api.fillSelection(argStr(a, "block")));
        tool("buildQueue",
                "Progress of the build queue that //set, //walls, //hollow, //cyl, //sphere, //replace and "
                + "buildBlocks feed: queued / placed / already / deferred (cells out of reach — walk closer with "
                + "gotoXYZ and re-issue for those) / done.",
                schema(), a -> api.buildQueue());
        tool("setBuilderWalks",
                "Who moves the bot while it builds. ON (default): the build queue walks itself to a position each "
                + "cell is placeable from, so it can reach a column top or a wall's far side. OFF: it places only "
                + "what is visible from where it stands and returns the rest in buildQueue().deferred for you to "
                + "reposition for yourself.",
                schema("on:bool"), a -> api.setBuilderWalks(argBool(a, "on")));
        tool("buildQueueClear",
                "Drop whatever the build queue still owes (wrong selection, changed plan).",
                schema(), a -> api.buildQueueClear());
        tool("wallsSelection",
                "//walls — fill only the 4 vertical walls of the selection (hollow interior).",
                schema("block:string"), a -> api.wallsSelection(argStr(a, "block")));
        tool("hollowSelection",
                "//hollow — fill the 6-face SHELL of the selection (walls + floor + ceiling), interior open.",
                schema("block:string"), a -> api.hollowSelection(argStr(a, "block")));
        tool("cylSelection",
                "//cyl — fill the solid cylinder inscribed in the selection (circle in XZ that fits the box, all Y).",
                schema("block:string"), a -> api.cylSelection(argStr(a, "block")));
        tool("sphereSelection",
                "//sphere — fill the solid sphere/ellipsoid inscribed in the selection (radii = the 3 half-extents).",
                schema("block:string"), a -> api.sphereSelection(argStr(a, "block")));
        tool("mineBlock",
                "Mine the block at (x,y,z) if in reach (tungsten break queue: tool equip + protection "
                + "+ gravity re-mine apply). Poll mineStatus; reposition (gotoXYZ) for out-of-reach.",
                schema("x:int", "y:int", "z:int"),
                a -> api.mineBlocks(java.util.List.of(java.util.List.of(argInt(a, "x"), argInt(a, "y"), argInt(a, "z")))));
        tool("mineStatus",
                "Poll the break queue: {mining, remaining}. mining=false && remaining=0 -> done.",
                schema(), a -> api.mineStatus());
        tool("cleanupScaffold",
                "Mine out the tungsten NAV scaffolding (pillar-up / bridge blocks it placed to reach a "
                + "goal) — the garbage around a build. Top-down, finite, can't loop. Poll mineStatus; "
                + "reposition (gotoXYZ) for out-of-reach.",
                schema(), a -> api.cleanupScaffold());
        tool("copySelection",
                "//copy — snapshot the non-air blocks of the selection into a clipboard (offsets from min).",
                schema(), a -> api.copySelection());
        tool("pasteClipboard",
                "//paste — place the clipboard at the player's block position. Poll buildBlocks-style remaining.",
                schema(), a -> api.pasteClipboard());
        tool("selectionSize",
                "//size — the selection's min/max/size/volume and clipboard count.",
                schema(), a -> api.selectionSize());
        tool("undoLast",
                "//undo — restore the last snapshot (auto-taken before set/replace/walls/hollow/cyl/sphere): "
                + "breaks the region then rebuilds the snapshot. Poll undoStatus.",
                schema(), a -> api.undoLast());
        tool("undoStatus",
                "Poll //undo: phase breaking -> placing -> done.",
                schema(), a -> api.undoStatus());
        tool("loadSchem",
                "//schem load — read a .schem/.schematic/.litematic from <gamedir>/schematics/ and BUILD it "
                + "anchored at the player (via buildBlocks). Download real ones from minecraft-schematics.com.",
                schema("name:string"), a -> api.loadSchem(argStr(a, "name")));
        tool("replaceSelection",
                "//replace — swap every selection cell that is `from` (or \"*\"/\"any\" = any non-air) for "
                + "`to`. Two phases: breaks the matching cells, then poll replaceStatus which places `to`. "
                + "Real survival placement. Reposition (gotoXYZ) if remaining stalls out of reach.",
                schema("from:string", "to:string"),
                a -> api.replaceSelection(argStr(a, "from"), argStr(a, "to")));
        tool("replaceStatus",
                "Poll //replace: phase = breaking -> placing -> done, with placed/remaining counts.",
                schema(), a -> api.replaceStatus());
        tool("buildBlocks",
                "Schematic placement: blocks = JSON array of [x,y,z,name] (world coords), placed bottom-up "
                + "in reach. Reposition (gotoXYZ) for `remaining`, call again. Parse a schematic into this "
                + "list; build-order + material sourcing are the agent's job.",
                schema("blocks:string"),
                a -> {
                    try {
                        com.google.gson.JsonArray arr = com.google.gson.JsonParser
                                .parseString(argStr(a, "blocks")).getAsJsonArray();
                        java.util.List<Object> blocks = new java.util.ArrayList<>();
                        for (com.google.gson.JsonElement e : arr) {
                            com.google.gson.JsonArray b = e.getAsJsonArray();
                            blocks.add(java.util.List.of(b.get(0).getAsInt(), b.get(1).getAsInt(),
                                    b.get(2).getAsInt(), b.get(3).getAsString()));
                        }
                        return api.buildBlocks(blocks);
                    } catch (Exception ex) {
                        return java.util.Map.of("ok", false, "reason", "bad blocks JSON: " + ex.getMessage());
                    }
                });
        tool("buildDefenseAround",
                "Box a cell with a protective shell (sides + roof) — e.g. defend a bed. Covers reachable cells; "
                + "reposition for the rest.",
                schema("x:int", "y:int", "z:int"),
                a -> api.buildDefenseAround(argInt(a, "x"), argInt(a, "y"), argInt(a, "z")));
        tool("canBreakBlock",
                "Predict whether the mod is allowed to mine a cell (protection policy: deny-list/zones, "
                + "claims, altoclef break-avoiders). Check before mining.",
                schema("x:int", "y:int", "z:int"),
                a -> booleanMap(api.canBreakBlock(argInt(a, "x"), argInt(a, "y"), argInt(a, "z"))));
        tool("canPlaceBlock",
                "Predict whether the mod is allowed to place at a cell (protection policy + replaceable). "
                + "Returns canPlace/policyAllows/replaceable. Check before building.",
                schema("x:int", "y:int", "z:int"),
                a -> api.canPlaceBlock(argInt(a, "x"), argInt(a, "y"), argInt(a, "z")));
        tool("markProtectedArea",
                "Mark a claim/private the mod must NOT build or mine in — a cube of radius r around (x,y,z). "
                + "The bot then routes and builds around it (claim convention).",
                schema("x:int", "y:int", "z:int", "r:int"),
                a -> api.markProtectedArea(argInt(a, "x"), argInt(a, "y"), argInt(a, "z"), argInt(a, "r")));
        tool("clearProtectedAreas",
                "Clear all runtime protected areas (place + break deny zones).",
                schema(), a -> api.clearProtectedAreas());

        // menus / shop / hotbar
        tool("selectHotbar", "Select hotbar slot 0-8.", schema("slot:int"),
                a -> booleanMap(api.selectHotbar(argInt(a, "slot"))));
        // ── companion primitives (2026-10-26): the three levers a playmate needs and the mod
        //    never had — see where the other player looks, hand something over, say it with the
        //    body. All three stay inside the "no chat output" rule.
        tool("playerDetail",
                "View of another player: yaw/pitch (where they look), held item, sneaking, hp, pos. "
                        + "Read before crowding them or while they are busy.",
                schema("player:string"), a -> api.playerDetail(argStr(a, "player")));
        tool("dropItem",
                "Hand an item to a player: faces them, drops `count` of `item` from the inventory "
                        + "(the drop keeps the look direction, so they pick it up). No chat involved.",
                schema("item:string", "count:int", "toward:string"),
                a -> api.dropItem(argStr(a, "item"), argInt(a, "count"), argStr(a, "toward")));
        tool("gesture",
                "Body language without chat: nod|agree, shake|disagree, jump|cheer.",
                schema("kind:string"), a -> api.gesture(argStr(a, "kind")));
        tool("clickMenuByName",
                "Click a menu slot by item display name (shop purchase / hub navigation). names = candidate labels "
                + "(first match wins), button 0=left/1=right, action e.g. 'PICKUP', timeoutMs to retry the menu read.",
                schema("names:strarray", "button:int", "action:string", "timeoutMs:int"),
                a -> {
                    List<String> ns = new ArrayList<>();
                    for (JsonElement e : a.getAsJsonArray("names")) ns.add(e.getAsString());
                    return intMap(api.clickMenuByName(ns, argInt(a, "button"), argStr(a, "action"), argInt(a, "timeoutMs")));
                });

        // commands / chat
        tool("ExecuteCommand",
                "Run an altoclef command (e.g. '@goto 10 64 20', '@get log 3', '@game', '@stop').",
                schema("cmd:string"), a -> { api.ExecuteCommand(argStr(a, "cmd")); return okMap(); });
        tool("ChatMessage",
                "Send chat / a tungsten command (';goto x y z', ';bridge', ';stop') or plain chat text.",
                schema("msg:string"), a -> { api.ChatMessage(argStr(a, "msg")); return okMap(); });
        tool("ConnectToServer",
                "Connect to a Minecraft server by address (e.g. 'mc.example.top' or 'test-server').",
                schema("ip:string"), a -> { api.ConnectToServer(argStr(a, "ip")); return okMap(); });
        tool("inGame", "Whether the bot is currently in a world (bool).", schema(),
                a -> booleanMap(api.inGame()));

        // ---- 2026-10-25 LOCAL PATCH: expose three abilities that already exist in Py4jEntryPoint
        //      but were never registered on the MCP surface. Without them the agent cannot use a
        //      bucket at all: placeBlockAt() swaps the hand to a BlockItem first (a bucket is not a
        //      BlockItem), so water-on-lava was unreachable from the MCP side.
        tool("lookAt",
                "Aim the crosshair at a BLOCK coordinate (aims at the block's CENTRE, NOT its corner). "
                + "The vanilla mouse pipeline applies the delta immediately (never a forged look "
                + "packet), but the raytrace agrees a tick later -- sleep ~150 ms before useHeldItem / "
                + "placeBlockLooking.",
                schema("x:int", "y:int", "z:int"),
                a -> booleanMap(api.lookAt(argInt(a, "x"), argInt(a, "y"), argInt(a, "z"))));
        tool("lookAtPoint",
                "Aim the crosshair at a world POINT (doubles): a player's eye, a liquid surface, an "
                + "entity's aim point. Use lookAt for block coordinates -- this one does NOT add 0.5.",
                schema("x:num", "y:num", "z:num"),
                a -> booleanMap(api.lookAtPoint(argDbl(a, "x"), argDbl(a, "y"), argDbl(a, "z"))));
        tool("useHeldItem",
                "Right-click with the HELD item: water bucket onto lava => obsidian, flint&steel to light "
                + "a portal. Aim with lookAt first. Touches no inventory, so it works with a bucket.",
                schema(), a -> api.useHeldItem());
        tool("placeBlockLooking",
                "Place the HELD block against whatever face the crosshair is on. No inventory touch.",
                schema(), a -> api.placeBlockLooking());

        // ---- 2026-10-26 NIGHT PATCH (Buddy): the companion layer needs eyes and a close button.
        //      Everything here already existed in Py4jEntryPoint but was never registered on the MCP
        //      surface, so the agent could not reach it.
        //
        //      THE ONE THAT MATTERS MOST IS closeScreen. tungsten's WindMouse aim returns every
        //      frame while `currentScreen != null` (WindMouseRotation.applyRenderStep, line 137),
        //      so a GUI left open means the crosshair never reaches the planned cell, the attack key
        //      is never pressed, and mining silently burns its whole 300-tick watchdog
        //      ("Mining aborted ... progress=-0.10" = getBlockBreakingProgress() == -1: never started).
        tool("closeScreen",
                "Close any open GUI (inventory / pause menu / chat). ALSO THE FIX for 'mining does "
                + "nothing': with a screen open the aim pipeline is frozen, so every dig silently aborts.",
                schema(), a -> booleanMap(api.closeOpenScreen()));
        tool("respawnPlayer",
                "Click the respawn button after death (also closes the death screen). No-op when alive. "
                + "A dead body with no respawn is indistinguishable from a frozen one from the outside.",
                schema(), a -> booleanMap(api.respawnPlayer()));
        tool("destroyBlockAt",
                "Break ONE block with altoclef's own DestroyBlockTask (it finds a visible face itself, "
                + "no crosshair needed). Slower than mineBlock but works when the aim pipeline is down.",
                schema("x:int", "y:int", "z:int"),
                a -> api.destroyBlockAt(argInt(a, "x"), argInt(a, "y"), argInt(a, "z")));
        tool("getEntities",
                "Non-player entities (mobs, dropped items) around the bot, nearest first: "
                + "type / distance / position / is_item. This is the guard-and-hero sense: without it "
                + "'protect the player' can only be executed blindly.",
                schema("limit:int"), a -> api.getEntitiesInfo(argInt(a, "limit")));
        tool("taskChain",
                "What altoclef is trying to do RIGHT NOW, one line per live chain (name, active, "
                + "priority) plus the current task tree. Reading it is how a supervisor tells "
                + "'busy' apart from 'wedged'.",
                schema(), a -> api.getTaskChain());
        tool("taskChainText",
                "Same as taskChain but pre-rendered as one string (cheaper to log).",
                schema(), a -> api.getTaskChainString());
        tool("stateDigest",
                "ONE call for a control-loop tick: self(hp/armor/food/pos/vel/onGround/dimension/held/"
                + "freeSlots/blocks/breakingProgress) + around(up/down/north/south/east/west block ids) "
                + "+ target(crosshair) + player(nearest other player) + entities(5 nearest) + "
                + "path(busy/goal/distance/arrived). Replaces ~6 separate perception calls per tick.",
                schema(), a -> api.stateDigest());
        tool("getBlocksBatch",
                "Read MANY blocks in ONE call: coords is a comma-separated list of \"x,y,z\" "
                + "(max 64). Returns {coord: blockId}. Cheaper than N × getBlockAt when checking a "
                + "route, a wall, or a build plan.",
                schema("coords:string"),
                a -> api.getBlocksBatch(java.util.Arrays.asList(
                        argStr(a, "coords").split(";"))));
        tool("raycastFromPlayer",
                "What the bot is looking at, up to maxDist blocks: crosshair hit first, then a longer "
                + "ray. Returns kind(block/entity/none) + block id + pos (+distance when raycast). "
                + "Read-only; use it before mineBlock/lookAt to confirm there IS a target.",
                schema("maxDist:int"),
                a -> api.raycastFromPlayer(argInt(a, "maxDist")));
        tool("scanBlocks",
                "Find BLOCKS BY NAME within a radius of the bot (e.g. 'iron_ore', 'oak_log'): "
                + "position / block id / distance, nearest first. The block-space equivalent of "
                + "getEntities -- 'is there any iron near me' was previously unanswerable.",
                schema("name:string", "radius:int", "limit:int"),
                a -> api.scanBlocks(argStr(a, "name"), argInt(a, "radius"), argInt(a, "limit")));
        tool("lookAtPlayer",
                "Aim at another player by name (uses the same mouse pipeline as lookAt).",
                schema("player:string"), a -> booleanMap(api.lookAtPlayer(argStr(a, "player"))));
        tool("attackPlayer",
                "Attack another player by name. Combat against PLAYERS only; use @hero for mobs.",
                schema("player:string"), a -> booleanMap(api.attackPlayer(argStr(a, "player"))));
        tool("voiceStatus",
                "Whether a voice-chat mod is connected (simple voice chat). Lets the companion know "
                + "whether it can speak/listen instead of only typing.",
                schema(), a -> booleanMap(api.isVoiceChatConnected()));

        tool("cancelAll",
                "Cancel everything the bot is doing (stop pathing + disable the task runner). "
                + "Use it before switching goals, or when a long job seems stuck.",
                schema(), a -> {
                    api.stopPathing();
                    api.ExecuteCommand("@stop");
                    return "{\"ok\":true,\"cancelled\":\"all\"}";
                });

        // ---- 2026-10-27 HOOK 层：不重造能力，直接把 AltoClef 自己的决策层露给 agent ----
        tool("listCommands",
                "LIST every built-in AltoClef command (name/description/arg count). The mod already "
                + "implements far more than the ~40 we hardcoded: deposit (put items in a container), "
                + "give (collect an item and hand it to a player), stash (store into a chest region), "
                + "wander, pause, custom, locate, ... Read this before writing anything by hand.",
                schema(), a -> api.listCommands());
        tool("obtainableItems",
                "LIST every resource name the bot's own catalogue can obtain (TaskCatalogue.resourceNames). "
                + "Use it to check 'can it even get this?' before asking for it.",
                schema(), a -> api.obtainableItems());
        tool("obtainItem",
                "OBTAIN an item using AltoClef's own ResourceTask (gather -> craft -> smelt -> find a table, "
                + "with its own failure recovery). This is the right lever for 'craft me a wooden sword': "
                + "do NOT re-implement crafting. Fire-and-poll: then watch userTask / taskChain.",
                schema("item:string", "count:int"),
                a -> api.obtainItem(argStr(a, "item"), argInt(a, "count")));
        tool("runCommandText",
                "RUN any built-in command by text (same set as listCommands), e.g. 'give Xiaorun oak_log 3' "
                + "or 'deposit'. Thin wrapper over the command executor; returns a structured result.",
                schema("cmd:string"), a -> api.runCommandText(argStr(a, "cmd")));
        tool("takeControl",
                "TAKE (on=true) or RETURN (on=false) control: on=true disables the bot's autonomous task "
                + "chains so it only does what you ask (use it when its own plans keep fighting yours).",
                schema("on:bool"), a -> api.takeControl(argBool(a, "on")));
        tool("userTask",
                "WHAT user task is running right now ('' = idle). This is how you tell 'my job finished' "
                + "from 'still working' after obtainItem/runCommandText.",
                schema(), a -> api.userTaskName());

        // ---- 2026-10-27 最后两个缺口：槽位点击（背包/容器）与"按数量从箱子取" ----
        tool("clickUiSlot",
                "CLICK a slot of the CURRENTLY OPEN screen by window index (0-based as returned by "
                + "getOpenScreen). button 0=LMB / 1=RMB; action PICKUP | QUICK_MOVE (shift) | THROW | SWAP. "
                + "This is the general primitive for moving items inside the inventory: two PICKUP "
                + "clicks (from-slot, then to-slot) = a move. Without it the only inventory lever was "
                + "selectHotbar, which cannot reorganise anything.",
                schema("windowSlot:int", "button:int", "action:string"),
                a -> booleanMap(api.clickUiSlot(argInt(a, "windowSlot"), argInt(a, "button"),
                        argStr(a, "action"))));
        tool("pickupFromContainer",
                "TAKE a specific item+count from a container at (x,y,z) using AltoClef's own "
                + "PickupFromContainerTask (walk there, open it, click the right slots, free a slot "
                + "first if needed). Use it instead of hand-rolling chest clicks.",
                schema("x:int", "y:int", "z:int", "item:string", "count:int"),
                a -> api.pickupFromContainer(argInt(a, "x"), argInt(a, "y"), argInt(a, "z"),
                        argStr(a, "item"), argInt(a, "count")));



    }

    // ---- helpers ----

    private static java.util.Map<String, Object> okMap() {
        java.util.Map<String, Object> m = new java.util.HashMap<>();
        m.put("ok", true);
        return m;
    }
    private static java.util.Map<String, Object> booleanMap(boolean b) {
        java.util.Map<String, Object> m = new java.util.HashMap<>();
        m.put("ok", b);
        return m;
    }
    private static java.util.Map<String, Object> intMap(int v) {
        java.util.Map<String, Object> m = new java.util.HashMap<>();
        m.put("result", v);
        return m;
    }

    private static int argInt(JsonObject a, String k) { return a.get(k).getAsInt(); }
    private static String argStr(JsonObject a, String k) { return a.get(k).getAsString(); }
    private static double argDbl(JsonObject a, String k) { return a.get(k).getAsDouble(); }
    private static boolean argBool(JsonObject a, String k) { return a.get(k).getAsBoolean(); }
    private static List<String> strList(JsonObject a, String k) {
        List<String> out = new ArrayList<>();
        if (a.has(k) && a.get(k).isJsonArray()) for (JsonElement e : a.getAsJsonArray(k)) out.add(e.getAsString());
        return out;
    }

    /** Build a JSON-Schema object from "name:type" pairs (int/string/bool/strarray). */
    private static JsonObject schema(String... pairs) {
        JsonObject props = new JsonObject();
        JsonArray required = new JsonArray();
        for (String p : pairs) {
            String[] kv = p.split(":");
            JsonObject prop = new JsonObject();
            switch (kv[1]) {
                case "int":    prop.addProperty("type", "integer"); break;
                case "num":    prop.addProperty("type", "number"); break;
                case "bool":   prop.addProperty("type", "boolean"); break;
                case "strarray":
                    prop.addProperty("type", "array");
                    JsonObject items = new JsonObject();
                    items.addProperty("type", "string");
                    prop.add("items", items);
                    break;
                default:       prop.addProperty("type", "string");
            }
            props.add(kv[0], prop);
            required.add(kv[0]);
        }
        JsonObject s = new JsonObject();
        s.addProperty("type", "object");
        s.add("properties", props);
        s.add("required", required);
        return s;
    }
}
