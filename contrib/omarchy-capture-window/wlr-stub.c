// The protocol's v2 request references zwlr_foreign_toplevel_handle_v1, so the
// generated table needs the symbol even though we only ever use the v1 request
// (capture_toplevel, by address). Pulling in all of wlr-foreign-toplevel-management
// to satisfy a pointer we never dereference would be the wrong trade.
#include <wayland-client.h>
const struct wl_interface zwlr_foreign_toplevel_handle_v1_interface = {
    "zwlr_foreign_toplevel_handle_v1", 3, 0, NULL, 0, NULL,
};
