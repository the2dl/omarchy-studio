// omarchy-studio-screenshare: keep `no_screen_share` windows OUT of a screenshare
// instead of covering them with black boxes.
//
// This is the plugin form of contrib/hyprland-screenshare-exclude/, which was an
// out-of-tree patch to Hyprland itself. Same mechanism, same cost, none of the
// "carry a compositor fork" tax: a patched compositor pins the package, blocks -Syu,
// has to be rebuilt for every release, and when it is eventually overwritten by an
// update the feature disappears silently. A plugin rebuilds with `hyprpm update`,
// and when it cannot load, the compositor is still the stock one.
//
// WHY THIS IS NEEDED AT ALL
//
// The camera bubble has to be visible on screen while recording and absent from the
// recording, so it stays a separate, editable stream. Stock Hyprland honours
// `no_screen_share` on the portal/screencopy path, but by painting Colors::BLACK
// over the window in CScreenshareFrame::renderMonitor(). A black box is survivable
// only if the bubble never moves; the whole point of the feature is that it does,
// so the hole moves with it and smears through the take.
//
// HOW IT WORKS
//
// Hyprland already renders to two colour attachments when CMonitor::needsUnmodifiedCopy()
// is true (the SH_FEAT_MIRROR path, added upstream for HDR/SDR screensharing):
// attachment 0 is the frame you see, attachment 1 is the mirror texture that
// saveBufferForMirror() blits into mirrorFB(), which is what screenshares read.
//
// So: force that MRT path on while a screenshare is live, mask attachment 1 around
// the excluded window's pass elements (glDrawBuffers with GL_NONE), and stop the
// black rect from being drawn. The window reaches attachment 0 and never reaches
// attachment 1, so the screenshare shows whatever is behind it. Nothing about what
// the user sees changes.
//
// WHAT IS API AND WHAT IS A HOOK
//
// Deliberately, as much as possible is public API -- the event bus, custom pass
// elements, IFramebuffer::getMirrorTexture(), CGLFramebuffer::getFBID(). Six function
// hooks remain, each on a seam the API does not expose: the black-rect loop (two),
// forcing the MRT path (one), bracketing one window's pass elements (one), and making
// sure what is BEHIND the window is drawn into the mirror at all (two, see below).
// Hooks are the part that can break on a Hyprland release, so the plugin refuses to
// load on a version mismatch rather than half-applying.

#include <hyprland/src/plugins/PluginAPI.hpp>
#include <hyprland/src/Compositor.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/pass/PassElement.hpp>
#include <hyprland/src/render/pass/ClearPassElement.hpp>
#include <hyprland/src/render/pass/SurfacePassElement.hpp>
#include <hyprland/src/render/gl/GLFramebuffer.hpp>
#include <hyprland/src/output/Monitor.hpp>
#include <hyprland/src/output/MonitorResources.hpp>
#include <hyprland/src/desktop/state/WindowState.hpp>
#include <hyprland/src/desktop/rule/windowRule/WindowRuleApplicator.hpp>
#include <hyprland/src/event/EventBus.hpp>

#include <GLES3/gl32.h>

#if defined(OMARCHY_STUDIO_FUNCHOOK)
#include <funchook.h>
#endif

#include <algorithm>
#include <array>
#include <cctype>
#include <string>
#include <unordered_map>
#include <vector>

inline HANDLE PHANDLE = nullptr;

// --- state -------------------------------------------------------------------

// Per monitor: is there a visible no_screen_share window on it right now, with a
// screenshare actually reading from it? Recomputed once per frame in render.pre.
inline std::unordered_map<const Monitor::CMonitor*, bool> g_excluding;

// True only for the duration of CScreenshareFrame::renderMonitor(), so the
// shouldRenderWindow hook can tell "we are drawing black boxes" from every other
// caller of that function -- of which there are many, and they must not be touched.
inline bool g_inScreenshareFrame = false;

inline CHyprSignalListener g_lRenderPre, g_lMonitorRemoved;

// --- hook backend -------------------------------------------------------------
//
// Hyprland's CFunctionHook is x86-64 ONLY. CFunctionHook::hook() opens with
// `#if !defined(__x86_64__) return false;` (src/plugins/HookSystem.cpp, unchanged in
// v0.56.1 and main) because the mechanism is an inline trampoline assembled from raw
// x86 opcodes, with the displaced prologue decoded by the udis86 x86 disassembler. On
// aarch64 every hook() returns false, so all six fail and the plugin dies in init with
// "hooks refused to install" -- which reads like a bug in this plugin and is not one.
//
// So the hooks go through a handle that keeps CFunctionHook's shape -- an m_original to
// call through -- with two backends behind it:
//
//   x86-64 : HyprlandAPI::createFunctionHook, exactly as before. It is the proven path
//            and Hyprland owns the unhooking when the plugin unloads.
//   else   : vendored funchook (vendor/funchook/), which emits an AArch64 trampoline,
//            relocates the displaced PC-relative instructions and flushes the i-cache.
//            Here WE own the unhooking -- see PLUGIN_EXIT, which must uninstall before
//            this .so is closed or the patched call sites jump into freed memory.
struct CStudioHook {
    void* m_source      = nullptr;
    void* m_destination = nullptr;
    void* m_original    = nullptr; // valid once installStudioHooks() has returned true
};

inline CStudioHook* g_hkRenderMonitor        = nullptr;
inline CStudioHook* g_hkShouldRenderWindow   = nullptr;
inline CStudioHook* g_hkNeedsUnmodifiedCopy  = nullptr;
inline CStudioHook* g_hkRenderWindow         = nullptr;
inline CStudioHook* g_hkSurfaceOpaqueRegion  = nullptr;
inline CStudioHook* g_hkDrawClear            = nullptr;

// Why the hooks could not be installed, for the notification and the throw.
inline std::string g_hookError;

static bool excludedWindow(const PHLWINDOW& w) {
    return w && w->m_ruleApplicator && w->m_ruleApplicator->noScreenShare().valueOrDefault();
}

static bool excludingOn(const Monitor::CMonitor* m) {
    if (!m)
        return false;
    const auto IT = g_excluding.find(m);
    return IT != g_excluding.end() && IT->second;
}

static bool excludingOn(const PHLMONITOR& m) {
    return excludingOn(m.get());
}

// --- the mask ----------------------------------------------------------------

// A custom pass element that toggles writes to the main framebuffer's mirror
// attachment. This is the plugin's whole trick, and it is exactly what the patch's
// CGLFramebuffer::setMirrorWrite() did -- reachable from outside because
// getMirrorTexture(), isAllocated() and getFBID() are all public.
//
// undiscardable() is required: CRenderPass::simplify() drops elements whose bounding
// box misses the damage, and an element with no geometry would be dropped on any
// frame where the bubble did not move. A dropped "mask on" would leak the window
// into the mirror; a dropped "mask off" would punch a hole in the visible frame.
class CStudioMirrorMaskElement : public IPassElement {
  public:
    CStudioMirrorMaskElement(bool enable) : m_enable(enable) {}
    virtual ~CStudioMirrorMaskElement() = default;

    virtual std::vector<UP<IPassElement>> draw() {
        const auto FB = g_pHyprRenderer->m_renderData.mainFB ? g_pHyprRenderer->m_renderData.mainFB : g_pHyprRenderer->m_renderData.currentFB;
        if (!FB || !FB->isAllocated() || !FB->getMirrorTexture())
            return {};   // no second attachment this frame: nothing to mask, and
                         // calling glDrawBuffers on attachment 1 would be an error

        const auto GLFB = dynamicPointerCast<Render::GL::CGLFramebuffer>(FB);
        if (!GLFB)
            return {};

        GLint prev = 0;
        glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &prev);
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, GLFB->getFBID());
        const GLenum DRAW_BUFFERS[] = {GL_COLOR_ATTACHMENT0, static_cast<GLenum>(m_enable ? GL_COLOR_ATTACHMENT1 : GL_NONE)};
        glDrawBuffers(2, DRAW_BUFFERS);
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, static_cast<GLuint>(prev));
        return {};
    }

    virtual bool needsLiveBlur() {
        return false;
    }
    virtual bool needsPrecomputeBlur() {
        return false;
    }
    virtual bool undiscardable() {
        return true;
    }
    virtual const char* passName() {
        return "CStudioMirrorMaskElement";
    }
    virtual ePassElementType type() {
        return EK_CUSTOM;
    }

  private:
    bool m_enable = false;
};

// --- forcing the MRT path ----------------------------------------------------

// The mask below only means anything if the frame is being rendered to TWO colour
// attachments, which is what needsUnmodifiedCopy() gates. So force it true for a
// monitor we are excluding on.
//
// This was originally done by setting `render:keep_unmodified_copy = 1`, which
// needsUnmodifiedCopy() checks before anything else -- and which needed no hook at
// all. That does not work here: Omarchy drives Hyprland from a LUA config, and
// `hyprctl keyword` is a no-op under the Lua config manager (verified in a nested
// session: the value read back unchanged). A hook is also simply more correct --
// per monitor and per frame, rather than a global flag with a window of time where
// it has been asked for but has not landed yet, which a one-shot capture like grim
// fits neatly inside.

typedef bool (*origNeedsUnmodifiedCopy)(void*);

static bool hkNeedsUnmodifiedCopy(void* thisptr) {
    if (const auto IT = g_excluding.find((Monitor::CMonitor*)thisptr); IT != g_excluding.end() && IT->second)
        return true;
    return (*(origNeedsUnmodifiedCopy)g_hkNeedsUnmodifiedCopy->m_original)(thisptr);
}

// --- hooks -------------------------------------------------------------------

typedef void (*origScreenshareRenderMonitor)(void*);
typedef bool (*origShouldRenderWindow)(void*, PHLWINDOW, PHLMONITOR);
typedef void (*origRenderWindow)(void*, PHLWINDOW, PHLMONITOR, const Time::steady_tp&, bool, Render::eRenderPassMode, bool, bool);

// CScreenshareFrame is not in the shipped headers, so `this` stays an opaque pointer.
// We never touch it -- the hook exists only to bracket the original call with a flag.
static void hkScreenshareRenderMonitor(void* thisptr) {
    g_inScreenshareFrame = true;
    (*(origScreenshareRenderMonitor)g_hkRenderMonitor->m_original)(thisptr);
    g_inScreenshareFrame = false;
}

// The black-rect loop in CScreenshareFrame::renderMonitor() skips any window that
// shouldRenderWindow() refuses, so refusing ours there deletes the black box without
// reimplementing the function. Outside that loop the hook is a straight passthrough.
static bool hkShouldRenderWindow(void* thisptr, PHLWINDOW w, PHLMONITOR m) {
    if (g_inScreenshareFrame && excludingOn(m) && excludedWindow(w))
        return false;
    return (*(origShouldRenderWindow)g_hkShouldRenderWindow->m_original)(thisptr, w, m);
}

// Brackets one window's pass elements with the mirror mask.
//
// This started out on the RENDER_PRE_WINDOW / RENDER_POST_WINDOW bus events, which
// needed no hook and looked much tidier. It is wrong: renderWindow() has early returns
// between the two emits, so RENDER_POST_WINDOW is NOT guaranteed to follow its PRE --
// measured in a nested session, PRE fired on every frame and POST fired zero times.
// The mask then stayed OFF for the remainder of the frame and everything drawn after
// the bubble missed the mirror, so the capture came back black rather than showing what
// was behind. Wrapping the call is what makes the pair symmetric on every exit path,
// which is exactly why the original patch used a CScopeGuard here.
static void hkRenderWindow(void* thisptr, PHLWINDOW w, PHLMONITOR m, const Time::steady_tp& time, bool decorate, Render::eRenderPassMode mode, bool ignorePosition,
                           bool standalone) {
    // `standalone` is a window-share render of this window on its own; masking there
    // would hide it from a capture that exists to show it.
    const bool MASK = !standalone && excludingOn(m) && excludedWindow(w);

    if (MASK)
        g_pHyprRenderer->addPassElement(makeUnique<CStudioMirrorMaskElement>(false));

    (*(origRenderWindow)g_hkRenderWindow->m_original)(thisptr, w, m, time, decorate, mode, ignorePosition, standalone);

    if (MASK)
        g_pHyprRenderer->addPassElement(makeUnique<CStudioMirrorMaskElement>(true));
}

// --- what is BEHIND the window has to actually be drawn ------------------------
//
// Masking the window out of the mirror only helps if the content under it reaches
// the mirror, and two things in a stock frame stop that:
//
// 1. Occlusion culling. CRenderPass::simplify() subtracts every later element's
//    opaqueRegion() from the damage of everything beneath it, so nothing is drawn
//    under an opaque window -- the mirror keeps whatever was there before the window
//    arrived (a FROZEN patch in the recording, or black right after the mirror texture
//    was (re)allocated). The self-view is an mpv window: opaque. So an excluded
//    window reports no opaque region while it is being excluded, and the pass draws
//    what is under it. The cost is repainting one bubble's worth of pixels per frame.
//
// 2. The background clear. misc:background_color is a CClearPassElement, and
//    CGLElementRenderer::draw(CClearPassElement) is glClearBufferfv(GL_COLOR, 0, ...):
//    draw buffer 0 only, never the mirror attachment. Anywhere the bare background
//    shows, the mirror stays at the transparent black clearAfterInvalidation() gave
//    it, which is a stock bug that keep_unmodified_copy=1 exhibits with no plugin
//    loaded at all -- and exactly what a test rig with a solid background measures.
//    Under a wallpaper (a layer surface, drawn by a mirror-aware shader) it never
//    shows. Repeating the clear on draw buffer 1 fixes it for excluding monitors.

typedef CRegion (*origSurfaceOpaqueRegion)(void*);

static CRegion hkSurfaceOpaqueRegion(void* thisptr) {
    const auto EL = static_cast<CSurfacePassElement*>(thisptr);
    if (excludingOn(EL->m_data.pMonitor.get()) && excludedWindow(EL->m_data.pWindow))
        return {};
    return (*(origSurfaceOpaqueRegion)g_hkSurfaceOpaqueRegion->m_original)(thisptr);
}

typedef void (*origDrawClear)(void*, WP<CClearPassElement>, const CRegion&);

static void hkDrawClear(void* thisptr, WP<CClearPassElement> element, const CRegion& damage) {
    (*(origDrawClear)g_hkDrawClear->m_original)(thisptr, element, damage);

    auto& rd = g_pHyprRenderer->m_renderData;
    if (!element || !excludingOn(rd.pMonitor.get()))
        return;

    // Only the main (MRT) framebuffer has a mirror attachment; a clear into a temp FB
    // (blur, snapshots) has nothing to mirror.
    const auto FB = rd.mainFB ? rd.mainFB : rd.currentFB;
    if (!FB || FB != rd.currentFB || !FB->isAllocated() || !FB->getMirrorTexture())
        return;

    // drawClear() has already converted the colour for the work buffer; the mirror is
    // an SDR sRGB texture, which for an SDR monitor is the same space.
    const auto&                  col = element->m_data.color;
    const std::array<GLfloat, 4> c   = {static_cast<GLfloat>(col.r), static_cast<GLfloat>(col.g), static_cast<GLfloat>(col.b), static_cast<GLfloat>(col.a)};

    if (!rd.damage.empty()) {
        rd.damage.forEachRect([&](const auto& RECT) {
            Render::GL::g_pHyprOpenGL->scissor(&RECT, rd.transformDamage);
            glClearBufferfv(GL_COLOR, 1, c.data());
        });
        Render::GL::g_pHyprOpenGL->scissor(nullptr);
    } else
        glClearBufferfv(GL_COLOR, 1, c.data());
}

// --- per-frame state ---------------------------------------------------------

static void onRenderPre(PHLMONITOR monitor) {
    if (!monitor)
        return;

    // Deliberately NOT gated on monitor->needsACopyFB(). That only flips true on the
    // frame a capture actually asks for pixels, and the mirror framebuffer is built by
    // the frame BEFORE the one that reads it -- so gating on it left the first captured
    // frame with no mirror texture at all, and CScreenshareFrame::renderMonitor returned
    // early without drawing anything. A one-shot capture (grim) is all first frames, so
    // it never worked once. Measured in a nested stock session: "copyFB=1 mirrorTex=0".
    //
    // "A visible no_screen_share window is on this monitor" is true for as long as the
    // bubble is up, so it is always at least a frame ahead of any capture. The cost is
    // the MRT path running while such a window exists and nothing records -- bounded,
    // because that window only exists during a take.
    bool exclude = false;
    for (auto const& w : Desktop::windowState()->windows()) {
        if (!w->m_isMapped || w->isHidden() || !excludedWindow(w))
            continue;
        if (!g_pHyprRenderer->shouldRenderWindow(w, monitor))
            continue;
        exclude = true;
        break;
    }

    auto& state = g_excluding[monitor.get()];
    if (state != exclude) {
        state = exclude;
        // The mirror holds a frame composited under the old rule. Rebuilding it is
        // what stops a stale bubble (or a stale hole) sitting in the capture until
        // something else happens to damage that region.
        monitor->resources()->invalidateMirrorFB();
    }
}

// --- plugin boilerplate ------------------------------------------------------

APICALL EXPORT std::string PLUGIN_API_VERSION() {
    return HYPRLAND_API_VERSION;
}

static void* findFunction(const std::string& name, const std::string& mustContain) {
    for (const auto& m : HyprlandAPI::findFunctionsByName(PHANDLE, name)) {
        if (m.demangled.contains(mustContain))
            return m.address;
    }
    return nullptr;
}

#if defined(OMARCHY_STUDIO_FUNCHOOK)
inline funchook_t* g_funchook = nullptr;
#endif

// Installs every hook or leaves none installed. On the funchook path that is literal:
// prepare() only stages, and the single install() call patches all six or none.
static bool installStudioHooks(const std::vector<CStudioHook*>& hooks) {
#if defined(OMARCHY_STUDIO_FUNCHOOK)
    g_funchook = funchook_create();
    if (!g_funchook) {
        g_hookError = "funchook_create failed";
        return false;
    }

    const auto FAIL = [&](const char* what) {
        g_hookError = std::string(what) + ": " + funchook_error_message(g_funchook);
        funchook_destroy(g_funchook);
        g_funchook = nullptr;
        return false;
    };

    for (auto* h : hooks) {
        // funchook_prepare() takes the target in and hands the trampoline back out
        // through the same pointer, which is exactly what m_original wants to be.
        h->m_original = h->m_source;
        if (funchook_prepare(g_funchook, &h->m_original, h->m_destination) != FUNCHOOK_ERROR_SUCCESS)
            return FAIL("funchook_prepare");
    }

    if (funchook_install(g_funchook, 0) != FUNCHOOK_ERROR_SUCCESS)
        return FAIL("funchook_install");

    return true;
#else
    for (auto* h : hooks) {
        const auto HOOK = HyprlandAPI::createFunctionHook(PHANDLE, h->m_source, h->m_destination);
        if (!HOOK || !HOOK->hook()) {
            g_hookError = "CFunctionHook::hook() refused";
            return false;
        }
        h->m_original = HOOK->m_original;
    }
    return true;
#endif
}

APICALL EXPORT PLUGIN_DESCRIPTION_INFO PLUGIN_INIT(HANDLE handle) {
    PHANDLE = handle;

    // __hyprland_api_get_hash() resolves to the SERVER's copy at load time;
    // __hyprland_api_get_client_hash() is the inline one compiled into this .so. Both
    // are "<git hash>_aq_<ver>_hu_<ver>...", so they compare like for like -- unlike
    // getHyprlandVersion().hash, which is the bare commit and never equals either.
    // Loading against a different build is not a warning, it is a segfault waiting for
    // the first vtable call, so this refuses rather than tries.
    if (std::string(__hyprland_api_get_hash()) != std::string(__hyprland_api_get_client_hash())) {
        HyprlandAPI::addNotification(PHANDLE, "[omarchy-studio] built against a different Hyprland; refusing to load", CHyprColor{1.0, 0.2, 0.2, 1.0}, 6000);
        throw std::runtime_error(std::string("version mismatch: server=[") + __hyprland_api_get_hash() + "] client=[" + __hyprland_api_get_client_hash() + "]");
    }

    const auto RENDER_MONITOR = findFunction("renderMonitor", "CScreenshareFrame::renderMonitor");
    const auto SHOULD_RENDER  = findFunction("shouldRenderWindow", "shouldRenderWindow(Hyprutils::Memory::CSharedPointer<Desktop::View::CWindow>, "
                                                                   "Hyprutils::Memory::CSharedPointer<Monitor::CMonitor>)");
    const auto NEEDS_COPY     = findFunction("needsUnmodifiedCopy", "CMonitor::needsUnmodifiedCopy");
    const auto RENDER_WINDOW  = findFunction("renderWindow", "IHyprRenderer::renderWindow");
    const auto OPAQUE_REGION  = findFunction("opaqueRegion", "CSurfacePassElement::opaqueRegion");
    const auto DRAW_CLEAR     = findFunction("draw", "CGLElementRenderer::draw(Hyprutils::Memory::CWeakPointer<CClearPassElement>");

    if (!RENDER_MONITOR || !SHOULD_RENDER || !NEEDS_COPY || !RENDER_WINDOW || !OPAQUE_REGION || !DRAW_CLEAR) {
        HyprlandAPI::addNotification(PHANDLE, "[omarchy-studio] could not find the screenshare functions to hook", CHyprColor{1.0, 0.2, 0.2, 1.0}, 6000);
        throw std::runtime_error("[omarchy-studio-screenshare] missing hook targets");
    }

    g_hkRenderMonitor       = new CStudioHook{RENDER_MONITOR, (void*)&hkScreenshareRenderMonitor};
    g_hkShouldRenderWindow  = new CStudioHook{SHOULD_RENDER, (void*)&hkShouldRenderWindow};
    g_hkNeedsUnmodifiedCopy = new CStudioHook{NEEDS_COPY, (void*)&hkNeedsUnmodifiedCopy};
    g_hkRenderWindow        = new CStudioHook{RENDER_WINDOW, (void*)&hkRenderWindow};
    g_hkSurfaceOpaqueRegion = new CStudioHook{OPAQUE_REGION, (void*)&hkSurfaceOpaqueRegion};
    g_hkDrawClear           = new CStudioHook{DRAW_CLEAR, (void*)&hkDrawClear};

    if (!installStudioHooks({g_hkRenderMonitor, g_hkShouldRenderWindow, g_hkNeedsUnmodifiedCopy, g_hkRenderWindow, g_hkSurfaceOpaqueRegion, g_hkDrawClear})) {
        HyprlandAPI::addNotification(PHANDLE, "[omarchy-studio] hooks refused to install: " + g_hookError, CHyprColor{1.0, 0.2, 0.2, 1.0}, 6000);
        throw std::runtime_error("[omarchy-studio-screenshare] hook install failed: " + g_hookError);
    }

    g_lRenderPre   = Event::bus()->m_events.render.pre.listen([](PHLMONITOR m) { onRenderPre(m); });
    // Monitors come and go (a DP hotplug, a headless output); their entry here would
    // otherwise outlive them and be consulted for whatever lands at the same address.
    g_lMonitorRemoved = Event::bus()->m_events.monitor.preRemoved.listen([](PHLMONITOR m) {
        if (m)
            g_excluding.erase(m.get());
    });

    return {"omarchy-studio-screenshare", "Excludes no_screen_share windows from screenshares instead of blacking them out", "omarchy-studio", "1.0"};
}

APICALL EXPORT void PLUGIN_EXIT() {
#if defined(OMARCHY_STUDIO_FUNCHOOK)
    // Ours to undo. Unlike the HyprlandAPI hooks, nothing else knows these exist, and
    // the patched call sites must stop pointing into this .so before it is dlclosed.
    // First, so no in-flight call enters plugin code after the state below is gone.
    if (g_funchook) {
        funchook_uninstall(g_funchook, 0);
        funchook_destroy(g_funchook);
        g_funchook = nullptr;
    }
#endif

    g_lRenderPre.reset();
    g_lMonitorRemoved.reset();
    g_excluding.clear();
}
