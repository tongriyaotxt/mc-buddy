package adris.altoclef.util.agent;

import kaptainwutax.tungsten.TungstenModDataContainer;
import kaptainwutax.tungsten.path.PathExecutor;

/**
 * HEADLESS BREAK — "is any real BLOCK WORK queued right now?" (2026-10-30)
 *
 * <p>⛔ WHY THIS EXISTS. {@code MinecraftClient.doItemUse()} ends with
 *
 * <pre>handleBlockBreaking(currentScreen == null &amp;&amp; !bl
 *     &amp;&amp; options.attackKey.isPressed()
 *     &amp;&amp; mouse.isCursorLocked());</pre>
 *
 * <p>The last term is FALSE on every client nobody has clicked into — and a companion client is
 * exactly that ("看不见但能干活"). With it false, vanilla calls {@code handleBlockBreaking(false)}
 * every tick, which calls {@code cancelBlockBreaking()}: the attack key tungsten presses has no
 * effect, the aim sits on the planned cell, and the break queue dies on its watchdog.
 *
 * <p>Measured on buddy11 (2026-10-30), {@code mineBlock} on a grass block 2.12 blocks away:
 *
 * <pre>[Tungsten] Mining aborted: ticks=302 dist=2.12 target=-7,83,8
 *            eye=(-6.50,85.62,8.50) est=20 budget=300 ground=true held=minecraft:air progress=-0.10</pre>
 *
 * <p>302 ticks = the queue ran the whole watchdog, the crosshair WAS on the target the whole time
 * ({@code lookingAt=grass_block@-7,83,8}), {@code getBlockBreakingProgress()} never left -1 — and a
 * real relative mouse move did not turn the camera, i.e. {@code isCursorLocked() == false}.
 *
 * <p>So the fix is one question asked at that one call site: <b>does anything actually want to
 * break/place a block?</b> Three producers, one answer:
 *
 * <ul>
 *   <li>tungsten's dig queue ({@code EXECUTOR.breakQueue}) — {@code mineBlock} / {@code @goto} digs;</li>
 *   <li>tungsten's place queue ({@code EXECUTOR.placeQueue}) — bridge / pillar;</li>
 *   <li>altoclef's own miner ({@code minerMineUntilMs}, stamped by {@code DestroyBlockTask}'s
 *       "Block in range, mining" branch) — {@code @mineblock} / {@code destroyBlockAt}.</li>
 * </ul>
 *
 * <p>Note this predicate only *unlocks the gate*; the attack key still has to be pressed by
 * whoever wants to dig, so no block is ever destroyed that the caller did not ask for.
 *
 * <p>WHY IT LIVES IN ALTOCLEF AND NOT IN TUNGSTEN: the release pipeline grafts the upstream
 * {@code tungsten-BETA-1.0.0.jar} ({@code tools/_make_hybrid_jar.py --donor}), so anything added to
 * the tungsten source tree is discarded at packaging time. Altoclef's own jar is ours.
 *
 * <p>Kill switch (no rebuild needed): {@code -Daltoclef.headlessCursorGate=false}.
 */
public final class HeadlessBreak {

    private static final boolean ENABLED =
            !"false".equalsIgnoreCase(System.getProperty("altoclef.headlessCursorGate", "true"));

    private HeadlessBreak() {
    }

    /** True while some block-breaking/placing job wants the hand. Never throws. */
    public static boolean wanted() {
        if (!ENABLED) {
            return false;
        }
        try {
            if (System.currentTimeMillis() < TungstenModDataContainer.minerMineUntilMs) {
                return true;                        // altoclef's own DestroyBlockTask is mining
            }
            PathExecutor ex = TungstenModDataContainer.EXECUTOR;
            if (ex == null) {
                return false;
            }
            if (ex.breakQueue != null && !ex.breakQueue.isEmpty()) {
                return true;
            }
            return ex.placeQueue != null && !ex.placeQueue.isEmpty();
        } catch (Throwable ignored) {
            // A diagnostic must never be able to break the client tick.
            return false;
        }
    }
}
