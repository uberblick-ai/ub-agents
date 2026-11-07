"""Rough seconds for the slowest tests, measured on Linux with eight workers.

These are scheduling hints, never selection rules or time limits. Refresh from
`python -m tests --durations 40` when the slow families change. Unknown tests
retain discovery order after these tests. See docs/development/testing.md.
"""

SLOW_TESTS = {
    'tests.test_view_terminal.TerminalRetentionTests.test_captured_log_pause_retention_and_generation_in_real_terminal': 16,
    'tests.test_view_terminal.TerminalViewTests.test_mouse_copy_and_y_in_real_terminal_restore_after_ctrl_c': 11,
    'tests.test_view_ui.ViewUITests.test_mode_shortcut_fits_all_tabs_and_preserves_active_styles_in_both_themes': 10,
    'tests.test_self_updates.SelfUpdateTests.test_configuration_pause_defers_upgrade_and_peer_restart_until_valid': 9,
    'tests.test_view_unblock_terminal.UnblockTerminalTests.test_unblock_in_real_terminal_q_and_ctrl_c_restore_terminal': 8,
    'tests.test_view_ui.ViewUITests.test_poll_key_on_every_tab_pane_and_overlay_only_for_attached_launcher': 8,
    'tests.test_view_recent.RecentActivityTerminalTests.test_real_terminal_wheel_input': 7,
    'tests.test_view_ui.ViewUITests.test_footer_hides_selection_hints_without_changing_navigation_or_help': 7,
    'tests.test_view_ui.ViewUITests.test_work_keyboard_cursor_only_visits_items_in_both_layouts': 6,
    'tests.test_view_ui.ViewUITests.test_description_requests_local_sources_cache_and_navigation': 6,
    'tests.test_launch_ui.LaunchTerminalTests.test_once_exits_and_restart_attaches_to_its_own_session': 5,
    'tests.test_view_ui.ViewUITests.test_header_reference_clicks_wide_dark': 5,
    'tests.test_view_ui.ViewUITests.test_header_reference_clicks_wide_light': 5,
    'tests.test_parallel.ParallelTests.test_blocked_protocol_reads_and_writes_do_not_block_the_deadline': 5,
    'tests.test_view_ui.ViewUITests.test_header_reference_clicks_narrow_dark': 5,
    'tests.test_view_ui.ViewUITests.test_recent_split_fixed_with_empty_and_overflowing_live_work': 5,
    'tests.test_view_refresh.ViewRefreshTests.test_idle_timer_and_each_visible_spinner_choose_the_cadence': 5,
    'tests.test_launch_ui.LaunchTerminalTests.test_q_and_sigterm_while_configuration_paused_exit_promptly_without_claims': 5,
    'tests.test_claude_terminal.ClaudeTerminalTests.test_oversized_image_error_and_raw_access_in_real_terminal': 5,
    'tests.test_view_unblock.UnblockUITests.test_omitted_notice_auto_loads_once_and_selection_with_active_tab_starts_no_reads': 5,
    'tests.test_view_ui.ViewUITests.test_pause_survives_sustained_reader_and_render_eviction_tabs_panes_raw_resize': 5,
    'tests.test_view_terminal.TerminalViewTests.test_single_pane_resize_navigation_floor_and_q_in_real_terminal': 5,
    'tests.test_view_terminal.TerminalViewTests.test_single_pane_resize_navigation_floor_and_interrupt_in_real_terminal': 5,
    'tests.test_view_ui.ViewUITests.test_idle_placeholder_is_one_dim_inert_line_and_running_stays_first': 5,
    'tests.test_help.HelpTests.test_cli_help_works_outside_a_repository_without_gh': 4,
    'tests.test_view_unblock.UnblockUITests.test_github_activation_read_shared_flight_cache_cooldown_and_trust_revocation': 4,
    'tests.test_view_ui.ViewUITests.test_awake_key_all_panes_and_overlays_waits_for_launcher_state': 4,
    'tests.test_view_ui.ViewUITests.test_issue_loads_on_keyboard_click_and_selection_with_cached_markdown': 4,
    'tests.test_view_terminal.TerminalViewTests.test_real_terminal_load_on_open_cache_and_interrupt_during_hung_request': 4,
    'tests.test_view_ui.ViewUITests.test_description_failure_explicit_retry_and_global_cooldown': 4,
    'tests.test_view_ui.ViewUITests.test_mouse_release_and_y_copy_exact_selection_in_panes_and_overlays': 4,
    'tests.test_view_unblock.UnblockUITests.test_activation_after_pending_read_or_cooldown_can_try_again_without_cached_result': 4,
    'tests.test_view_ui.ViewUITests.test_quit_closes_pending_recording_transport_for_both_keys': 4,
    'tests.test_finalization.FinalizationTests.test_every_post_exit_request_recovers_in_one_later_pass': 4,
    'tests.test_terminal.TerminalHarnessTests.test_escape_delay_from_spawned_environment_prevents_alt_keys': 4,
    'tests.test_view_terminal.TerminalViewTests.test_real_terminal_load_on_open_cache_and_q_during_hung_request': 4,
    'tests.test_view_recent.RecentActivityTests.test_wheel_click_last_log_arrows_and_independent_halves': 4,
    'tests.test_view_ui.ViewUITests.test_markdown_body_from_every_source_is_formatted_and_inert': 4,
    'tests.test_view_ui.ViewUITests.test_pointers_from_every_source_survive_main_body_limit_and_are_inert': 4,
    'tests.test_launch_ui.LaunchTerminalTests.test_q_drains_the_active_run_through_report_and_cleanup': 4,
}
