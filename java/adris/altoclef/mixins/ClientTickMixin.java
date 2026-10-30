package adris.altoclef.mixins;

import adris.altoclef.eventbus.EventBus;
import adris.altoclef.eventbus.events.ClientTickEvent;
import adris.altoclef.util.agent.HeadlessBreak;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.Mouse;
import net.minecraft.client.world.ClientWorld;
//#if MC < 12111
import net.minecraft.client.gui.screen.DownloadingTerrainScreen;
//#endif
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

// Changed this from player to client, I hope this doesn't break anything.
@Mixin(MinecraftClient.class)
public final class ClientTickMixin {

    @Shadow
    public ClientWorld world;

    @Inject(
            method = "tick",
            at = @At("HEAD")
    )
    private void clientTick(CallbackInfo ci) {
        EventBus.publish(new ClientTickEvent());
    }

    /**
     * HEADLESS BREAK (2026-10-30) — see {@link HeadlessBreak} for the whole measurement.
     *
     * <p>{@code doItemUse()} decides block breaking with
     * {@code currentScreen == null && !bl && attackKey.isPressed() && mouse.isCursorLocked()}.
     * A companion client is never clicked into, so that last term is false, vanilla cancels the
     * break every tick, and no block can be mined while the bot is "invisible" — which is the
     * default deployment. This redirect answers "locked" for THIS call only, and only while real
     * block work is queued; the user's cursor is never captured and every other reader of
     * {@code isCursorLocked()} still sees the truth.
     *
     * <p>{@code require = 0}: older MC targets (1.16.5 / 1.18.2 / 1.20.4) may not have this exact
     * call shape; there the injector simply does not apply, which is the pre-fix behaviour.
     */
    @Redirect(method = "doItemUse", require = 0,
            at = @At(value = "INVOKE", target = "Lnet/minecraft/client/Mouse;isCursorLocked()Z"))
    private boolean altoclef$headlessCursorLocked(Mouse mouse) {
        if (HeadlessBreak.wanted()) {
            return true;
        }
        return mouse != null && mouse.isCursorLocked();
    }

    // --- shredder joinWorld world-event bridge (preprocessed for cross-version) ---

    @Inject(method = "joinWorld", at = @At("HEAD"))
    //#if MC < 12111
    private void shredderPreLoadWorld(ClientWorld world, DownloadingTerrainScreen.WorldEntryReason arg2, CallbackInfo ci) {
    //#else
    //$$ private void shredderPreLoadWorld(ClientWorld world, CallbackInfo ci) {
    //#endif
    }

    @Inject(method = "joinWorld", at = @At("RETURN"))
    //#if MC < 12111
    private void shredderPostLoadWorld(ClientWorld world, DownloadingTerrainScreen.WorldEntryReason arg2, CallbackInfo ci) {
    //#else
    //$$ private void shredderPostLoadWorld(ClientWorld world, CallbackInfo ci) {
    //#endif
    }

    // fireShredderWorldEvent REMOVED (G-0): it existed to tell the legacy engine that a
    // world had loaded or unloaded. There is no legacy engine to tell.
}
