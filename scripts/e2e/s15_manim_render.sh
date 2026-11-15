#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# s15 Manim: no plan limit; two reserves are both granted (the render and its retry). Usage events need a real render: not run here.
scenario "s15 Manim reserves are granted (render and retry)"
need_stack
register_user manim || exit 1
become pro || exit 1
OUT="$(reserve "$USER_ID" - 2 manim)"
assert_eq "$(count_where "$OUT" 'd["allowed"]')" 2 "both Manim reserves granted"
me
assert_eq "$(jget "$HTTP_BODY" 'd["usage_today"]["total"]')" 0 "a Manim reserve counts no chat message"
reconcile_check
