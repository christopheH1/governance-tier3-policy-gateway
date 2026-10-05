package ai_governance.tier3.tool_egress_test

import rego.v1

import data.ai_governance.tier3.tool_egress

analyst := {"verified": true, "agent_id": "a1", "role": "analyst"}

processor := {"verified": true, "agent_id": "p1", "role": "data_processor"}

reporting := {"purpose": "sales_reporting", "task_id": "t1"}

decide(identity, intent, tool) := d if {
	d := tool_egress.decision with input as {"identity": identity, "intent": intent, "tool": tool}
}

test_allow_permitted_read if {
	decide(analyst, reporting, {"name": "database_read", "params": {"limit": 10}}).outcome == "allow"
}

test_deny_tool_not_whitelisted if {
	d := decide(analyst, reporting, {"name": "database_delete", "params": {}})
	d.outcome == "deny"
	"tool_not_whitelisted" in d.reasons
}

test_deny_unregistered_tool if {
	d := decide(analyst, reporting, {"name": "shell_exec", "params": {}})
	d.outcome == "deny"
	"tool_not_registered" in d.reasons
}

test_deny_unverified_identity if {
	d := decide({"verified": false, "role": "analyst"}, reporting, {"name": "database_read", "params": {}})
	d.outcome == "deny"
	"identity_not_verified" in d.reasons
}

test_deny_identity_missing if {
	d := decide({}, reporting, {"name": "database_read", "params": {}})
	d.outcome == "deny"
	"identity_not_verified" in d.reasons
}

test_deny_missing_intent if {
	d := decide(analyst, {}, {"name": "database_read", "params": {}})
	d.outcome == "deny"
	"intent_not_declared" in d.reasons
}

test_deny_unknown_purpose if {
	d := decide(analyst, {"purpose": "curiosity", "task_id": "t1"}, {"name": "database_read", "params": {}})
	d.outcome == "deny"
	"intent_not_recognised" in d.reasons
}

test_deny_intent_tool_mismatch if {
	d := decide(analyst, {"purpose": "market_research", "task_id": "t1"}, {"name": "database_read", "params": {}})
	d.outcome == "deny"
	"intent_tool_mismatch" in d.reasons
}

test_deny_empty_input if {
	d := tool_egress.decision with input as {}
	d.outcome == "deny"
}

test_refer_flag_large_read if {
	d := decide(analyst, reporting, {"name": "database_read", "params": {"limit": 50000}})
	d.outcome == "refer"
	d.mode == "flag"
	"row_threshold" in d.reasons
}

test_refer_hold_export if {
	d := decide(processor, {"purpose": "data_export", "task_id": "t1"}, {"name": "data_export", "params": {}})
	d.outcome == "refer"
	d.mode == "hold"
	"sensitive_tool" in d.reasons
}

test_refer_hold_unclassified_tool if {
	d := tool_egress.decision with input as {
		"identity": analyst,
		"intent": reporting,
		"tool": {"name": "database_read", "params": {}},
	}
		with data.tier3.tools.database_read as {"location": "internal"}
	d.outcome == "refer"
	d.mode == "hold"
	"tool_unclassified" in d.reasons
}

test_deny_takes_precedence_over_refer if {
	d := decide(analyst, reporting, {"name": "data_export", "params": {}})
	d.outcome == "deny"
}

test_destination_comes_from_registry if {
	d := tool_egress.destination with input as {"tool": {"name": "database_read"}}
	d == "http://mock-tools:8000/database/read"
}

test_no_destination_for_unknown_tool if {
	not tool_egress.destination with input as {"tool": {"name": "shell_exec"}}
}

test_hold_timeout_default if {
	t := tool_egress.hold_timeout_seconds with input as {"tool": {"name": "data_export"}}
	t == 900
}

test_hold_timeout_tool_override if {
	t := tool_egress.hold_timeout_seconds with input as {"tool": {"name": "data_export"}}
		with data.tier3.tools.data_export.hold_timeout_seconds as 60
	t == 60
}

test_hold_timeout_fallback_without_registry_value if {
	t := tool_egress.hold_timeout_seconds with input as {"tool": {"name": "data_export"}}
		with data.tier3.referral as {"tools": ["data_export"], "max_rows": 10000}
	t == 900
}
