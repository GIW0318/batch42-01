import pytest

from sudoers_alias_resolve import SudoersCompiler, SudoersError
import sudoers_alias_resolve


def test_alias_cycle_is_a_compile_error():
    compiler = SudoersCompiler()
    text = (
        "User_Alias A = B\n"
        "User_Alias B = A\n"
        "alice ALL = /bin/ls\n"
    )
    with pytest.raises(SudoersError) as exc:
        compiler.compile(text)
    assert exc.value.code == "ALIAS_CYCLE"


def test_deny_rule_wins_over_allow_all():
    compiler = SudoersCompiler()
    compiler.compile("alice ALL = ALL, !/bin/rm\n")
    assert compiler.check("alice", "web1", "/bin/rm")["allowed"] is False
    assert compiler.check("alice", "web1", "/bin/ls")["allowed"] is True


def test_user_and_host_aliases_share_names_without_collision():
    compiler = SudoersCompiler()
    compiler.compile(
        "User_Alias OPS = alice\n"
        "Host_Alias OPS = web1\n"
        "OPS OPS = /bin/ls\n"
    )
    assert compiler.check("alice", "web1", "/bin/ls")["allowed"] is True
    assert compiler.check("bob", "web1", "/bin/ls")["allowed"] is False
    assert compiler.check("alice", "db1", "/bin/ls")["allowed"] is False


def test_command_wildcard_does_not_allow_argument_injection():
    compiler = SudoersCompiler()
    compiler.compile("alice ALL = /usr/bin/*\n")
    assert compiler.check("alice", "web1", "/usr/bin/ls", ["/tmp/x"])["allowed"] is True
    assert compiler.check("alice", "web1", "/usr/bin/ls", ["; rm -rf /"])["allowed"] is False


def test_compile_files_applies_given_order():
    compiler = SudoersCompiler()
    compiler.compile_files(
        [
            ("10-base", "alice ALL = /bin/ls\n"),
            ("20-more", "alice ALL = /bin/cat\n"),
        ]
    )
    assert compiler.check("alice", "web1", "/bin/ls")["allowed"] is True
    assert compiler.check("alice", "web1", "/bin/cat")["allowed"] is True


def test_nested_group_members_are_resolved_to_all_levels():
    compiler = SudoersCompiler()
    compiler.set_groups({"alice": ["g1"], "g1": ["g2"], "g2": ["g3"]})
    compiler.compile(
        "User_Alias TEAM = %g3\n"
        "TEAM ALL = /bin/ls\n"
    )
    assert compiler.check("alice", "web1", "/bin/ls")["allowed"] is True


def test_denied_command_cannot_be_bypassed_with_dot_or_parent_paths():
    compiler = SudoersCompiler()
    compiler.compile("alice ALL = ALL, !/bin/rm\n")
    assert compiler.check("alice", "web1", "/bin/rm")["allowed"] is False
    assert compiler.check("alice", "web1", "/bin/../bin/rm")["allowed"] is False
    assert compiler.check("alice", "web1", "/bin/./ls")["allowed"] is True


def test_nopasswd_tag_does_not_leak_to_next_rule():
    compiler = SudoersCompiler()
    compiler.compile(
        "alice ALL=(ALL) NOPASSWD: /bin/ls\n"
        "alice ALL=(ALL) /bin/cat\n"
    )
    assert compiler.check("alice", "web1", "/bin/ls")["nopasswd"] is True
    assert compiler.check("alice", "web1", "/bin/cat")["nopasswd"] is False


def test_security_defaults_win_over_user_specific_override():
    compiler = SudoersCompiler()
    compiler.compile(
        "Defaults:alice !authenticate\n"
        "Defaults authenticate\n"
    )
    assert compiler.effective_defaults("alice")["authenticate"] is True


def test_cache_is_keyed_by_content_hash():
    compiler = SudoersCompiler()
    first = compiler.compile("alice ALL = /bin/ls\n")
    same = compiler.compile("alice ALL = /bin/ls\n")
    changed = compiler.compile("alice ALL = /bin/cat\n")
    assert first["digest"] == same["digest"]
    assert changed["digest"] != first["digest"]


def test_audit_records_full_command_and_arguments():
    compiler = SudoersCompiler()
    compiler.compile("alice ALL = /bin/ls\n")
    compiler.check("alice", "web1", "/bin/ls", ["-l", "/tmp/x"])
    event = compiler.audit_events()[-1]
    assert event["command"] == "/bin/ls"
    assert event["args"] == ["-l", "/tmp/x"]
    assert event["user"] == "alice"


def test_syntax_error_is_not_silently_skipped():
    compiler = SudoersCompiler()
    with pytest.raises(SudoersError) as exc:
        compiler.compile("alice ALL = /bin/ls\nTHIS IS BAD\n")
    assert exc.value.code == "SYNTAX_ERROR"


def test_deny_via_command_alias_has_priority():
    compiler = SudoersCompiler()
    compiler.compile(
        "Cmnd_Alias BAD = /bin/rm\n"
        "alice ALL = ALL, !BAD\n"
    )
    assert compiler.check("alice", "web1", "/bin/rm")["allowed"] is False
    assert compiler.check("alice", "web1", "/bin/ls")["allowed"] is True


def test_host_alias_matching_is_exact():
    compiler = SudoersCompiler()
    compiler.compile("alice web1 = /bin/ls\n")
    assert compiler.check("alice", "web1", "/bin/ls")["allowed"] is True
    assert compiler.check("alice", "web10", "/bin/ls")["allowed"] is False


def test_later_file_denial_overrides_earlier_allow():
    compiler = SudoersCompiler()
    compiler.compile_files(
        [
            ("10-base", "alice ALL = /bin/ls\n"),
            ("20-deny", "alice ALL = !/bin/ls\n"),
        ]
    )
    assert compiler.check("alice", "web1", "/bin/ls")["allowed"] is False
