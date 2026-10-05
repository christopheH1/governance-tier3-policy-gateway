package ai_governance.tier3.model_egress_test

import rego.v1

import data.ai_governance.tier3.model_egress

analyst := {"verified": true, "agent_id": "a1", "role": "analyst"}

reporting := {"purpose": "sales_reporting", "task_id": "t1"}

quiet := {"requests_this_minute": 1, "tokens_today": 0}

request(identity, intent, model, usage) := {"identity": identity, "intent": intent, "model": model, "usage": usage}

test_allow_permitted_model if {
	d := model_egress.decision with input as request(analyst, reporting, {"name": "claude-haiku", "max_tokens": 200}, quiet)
	d.outcome == "allow"
}

test_deny_model_outside_role if {
	d := model_egress.decision with input as request(
		{"verified": true, "role": "data_processor"}, reporting,
		{"name": "claude-haiku", "max_tokens": 200}, quiet,
	)
	d.outcome == "deny"
	"model_not_permitted" in d.reasons
}

test_deny_unregistered_model if {
	d := model_egress.decision with input as request(analyst, reporting, {"name": "some-other-model", "max_tokens": 10}, quiet)
	d.outcome == "deny"
	"model_not_registered" in d.reasons
}

test_deny_unverified_identity if {
	d := model_egress.decision with input as request({"verified": false, "role": "analyst"}, reporting, {"name": "mock-model", "max_tokens": 10}, quiet)
	d.outcome == "deny"
	"identity_not_verified" in d.reasons
}

test_deny_missing_intent if {
	d := model_egress.decision with input as request(analyst, {}, {"name": "mock-model", "max_tokens": 10}, quiet)
	d.outcome == "deny"
	"intent_not_declared" in d.reasons
}

test_deny_max_tokens_over_ceiling if {
	d := model_egress.decision with input as request(analyst, reporting, {"name": "claude-haiku", "max_tokens": 5000}, quiet)
	d.outcome == "deny"
	"max_tokens_exceeded" in d.reasons
}

test_rate_limit_boundary if {
	at_limit := model_egress.decision with input as request(analyst, reporting, {"name": "claude-haiku", "max_tokens": 10}, {"requests_this_minute": 30, "tokens_today": 0})
	at_limit.outcome == "allow"
	over := model_egress.decision with input as request(analyst, reporting, {"name": "claude-haiku", "max_tokens": 10}, {"requests_this_minute": 31, "tokens_today": 0})
	"rate_limit_exceeded" in over.reasons
}

test_deny_token_budget_spent if {
	d := model_egress.decision with input as request(analyst, reporting, {"name": "mock-model", "max_tokens": 10}, {"requests_this_minute": 1, "tokens_today": 200000})
	d.outcome == "deny"
	"token_budget_exceeded" in d.reasons
}

test_deny_empty_input if {
	d := model_egress.decision with input as {}
	d.outcome == "deny"
}

test_offered_tools_are_filtered if {
	tools := model_egress.permitted_tools with input as request(
		analyst, reporting,
		{"name": "mock-model", "max_tokens": 10, "offered_tools": ["database_read", "api_call", "data_export", "shell_exec"]}, quiet,
	)

	# database_read: in the role and in the purpose. api_call: in the role, not the purpose.
	# data_export: not in the role. shell_exec: not registered.
	tools == {"database_read"}
}

test_max_tokens_ceiling_fact if {
	m := model_egress.max_tokens with input as {"model": {"name": "claude-haiku"}}
	m == 1024
}

# --- the check that a request fits its declared purpose ------------------------
test_intent_check_uses_the_control_plane_checker if {
	c := model_egress.intent_check with input as request(analyst, reporting, {"name": "claude-haiku"}, quiet)
	c.via == "control"
	not c.model
	contains(c.description, "sales")
}

test_intent_check_uses_the_purpose_override if {
	c := model_egress.intent_check with input as request(analyst, {"purpose": "test_intent_check", "task_id": "t"}, {"name": "mock-model"}, quiet)
	c.via == "adapter"
	c.model == "scripted-checker"
}

test_intent_check_not_offered_when_switched_off if {
	not model_egress.intent_check with input as request(analyst, reporting, {"name": "claude-haiku"}, quiet)
		with data.tier3.intent_check as {"enabled": false}
}

test_intent_check_never_uses_an_external_checker if {
	not model_egress.intent_check with input as request(analyst, {"purpose": "test_intent_check", "task_id": "t"}, {"name": "mock-model"}, quiet)
		with data.tier3.purposes.test_intent_check.check_model as "claude-haiku"
}

test_intent_check_needs_a_purpose_description if {
	not model_egress.intent_check with input as request(analyst, {"purpose": "test_fixture", "task_id": "t"}, {"name": "claude-haiku"}, quiet)
}

test_flagged_text_is_recorded_only_when_switched_on if {
	on := model_egress.intent_check with input as request(analyst, reporting, {"name": "claude-haiku"}, quiet)
		with data.tier3.intent_check as {"enabled": true, "record_flagged_text": true}
	on.record_text == true
	off := model_egress.intent_check with input as request(analyst, reporting, {"name": "claude-haiku"}, quiet)
		with data.tier3.intent_check as {"enabled": true}
	off.record_text == false
}
