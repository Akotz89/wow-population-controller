#include "pbguide/gear_plan_json.hpp"
#include "json_contract.hpp"
#include <regex>
#include <tuple>

#include <boost/json.hpp>

#include <algorithm>
#include <limits>
#include <set>
#include <stdexcept>
#include <string>

namespace pbguide
{
namespace
{
namespace json = boost::json;

char const* DispositionName(GearDisposition disposition)
{
    switch (disposition)
    {
        case GearDisposition::Pursue:
            return "pursue";
        case GearDisposition::Deferred:
            return "deferred";
        case GearDisposition::Abandoned:
            return "abandoned";
    }
    throw std::invalid_argument("invalid gear disposition");
}

char const* TargetStatusName(GearTargetStatus status)
{
    switch (status)
    {
        case GearTargetStatus::Candidate:
            return "candidate";
        case GearTargetStatus::Invalid:
            return "invalid";
        case GearTargetStatus::Unavailable:
            return "unavailable";
    }
    throw std::invalid_argument("invalid gear target status");
}

json::array StringArray(std::vector<std::string> const& values)
{
    json::array result;
    result.reserve(values.size());
    for (std::string const& value : values)
        result.emplace_back(value);
    return result;
}

json::value OptionalString(std::optional<std::string> const& value)
{
    return value.has_value() ? json::value(*value) : json::value(nullptr);
}

json::value OptionalUint32(std::optional<std::uint32_t> const& value)
{
    return value.has_value() ? json::value(*value) : json::value(nullptr);
}

bool TryAdd(Score left, Score right, Score& output)
{
    if ((right > 0 && left > std::numeric_limits<Score>::max() - right) ||
        (right < 0 && left < std::numeric_limits<Score>::min() - right))
        return false;
    output = left + right;
    return true;
}
}

static std::string EncodePlan(GearPlan const& plan);

std::vector<std::string> ValidateGearPlanForPersistence(GearPlan const& plan)
{
    std::vector<std::string> errors;
    bool const anySelected = plan.selectedTargetId.has_value() || plan.selectedSourceId.has_value() ||
        plan.selectedItemId.has_value();
    bool const allSelected = plan.selectedTargetId.has_value() && plan.selectedSourceId.has_value() &&
        plan.selectedItemId.has_value();
    if (anySelected != allSelected)
        errors.push_back("partial_selected_identity");

    if (plan.planId != "gear_plan:" + std::to_string(plan.characterId) + ":" + std::to_string(plan.createdAtSeconds))
        errors.push_back("plan_identity_mismatch");
    // Invalid diagnostic records may repeat an ID or share it with one actionable
    // record. Only the unique actionable record can be referenced by evaluations.
    std::map<std::string, GearTargetEvaluation const*> targetById;
    for (GearTargetEvaluation const& target : plan.targets)
    {
        if (target.status == GearTargetStatus::Invalid) {
            if (std::find(target.reasonCodes.begin(), target.reasonCodes.end(), "invalid_upgrade_target") == target.reasonCodes.end())
                errors.push_back("invalid_target_missing_reason");
            continue;
        }
        if (target.stableId.size() > 160 || !std::regex_match(target.stableId, std::regex("[a-z][a-z0-9_-]*:[a-zA-Z0-9_.:-]+")) ||
            target.itemId == 0 || target.slot > 18 || target.playerbotsUpgradeScore <= 0 ||
            target.professionValue < 0 || target.expectedReplacementLevel == 0 || target.expectedReplacementLevel > 80)
            errors.push_back("invalid_actionable_target");
        if (!targetById.emplace(target.stableId, &target).second)
            errors.push_back("duplicate_target_id");
    }

    GearSourceEvaluation const* bestEligible = nullptr;
    for (auto const& evaluation : plan.evaluations)
    {
        if (!evaluation.eligible)
            continue;
        if (!bestEligible || evaluation.score.total > bestEligible->score.total ||
            (evaluation.score.total == bestEligible->score.total &&
                std::tie(evaluation.targetId, evaluation.stableId) <
                    std::tie(bestEligible->targetId, bestEligible->stableId)))
            bestEligible = &evaluation;
    }
    if (allSelected && (!bestEligible || plan.selectedTargetId != bestEligible->targetId ||
            plan.selectedSourceId != bestEligible->stableId || plan.selectedItemId != bestEligible->itemId))
        errors.push_back("selection_not_best_eligible");

    std::size_t pursueCount = 0;
    std::set<std::pair<std::string, std::string>> evaluationIds;
    for (GearSourceEvaluation const& evaluation : plan.evaluations)
    {
        auto const target = targetById.find(evaluation.targetId);
        if (target == targetById.end() || target->second->status != GearTargetStatus::Candidate)
            errors.push_back("evaluation_target_not_candidate");
        else if (evaluation.itemId != target->second->itemId)
            errors.push_back("evaluation_item_mismatch");
        if (!evaluationIds.emplace(evaluation.targetId, evaluation.stableId).second)
            errors.push_back("duplicate_evaluation:" + evaluation.targetId + ":" + evaluation.stableId);
        if (evaluation.disposition == GearDisposition::Pursue)
        {
            ++pursueCount;
            if (!evaluation.eligible) errors.push_back("pursue_ineligible");
            if (!allSelected || plan.selectedTargetId != evaluation.targetId ||
                plan.selectedSourceId != evaluation.stableId || plan.selectedItemId != evaluation.itemId)
                errors.push_back("pursue_selection_mismatch");
        }
        if (evaluation.eligible && evaluation.disposition == GearDisposition::Abandoned)
            errors.push_back("eligible_source_abandoned:" + evaluation.stableId);
        if (evaluation.eligible)
        {
            for (auto const& reason : evaluation.reasonCodes)
            {
                if (reason != "selected_highest_value" && reason != "below_pursuit_threshold" &&
                    reason != "craft_resources_feasible" &&
                    !std::regex_match(reason, std::regex("resource_procurement:[a-z][a-z0-9_-]*:[a-zA-Z0-9_.:-]+:[1-9][0-9]*")) &&
                    !std::regex_match(reason, std::regex("lower_ranked_than:[a-z][a-z0-9_-]*:[a-zA-Z0-9_.:-]+")))
                    errors.push_back("eligible_source_blocking_reason");
            }
        }
        auto const& score = evaluation.score;
        if (score.upgradeUtility < 0 || score.usefulLife < 0 || score.profession < 0 ||
            score.acquisitionTime > 0 || score.money > 0 || score.risk > 0 || score.contention > 0)
            errors.push_back("score_component_sign");
        if (evaluation.eligible && (score.expectedAttemptsNumerator == 0 || score.expectedAttemptsCeiling == 0))
            errors.push_back("eligible_score_counter_zero");
        if (score.expectedAcquisitionSeconds < 0 || score.expectedAttemptsDenominator == 0)
            errors.push_back("score_counter_invalid");
        else if (score.expectedAttemptsCeiling != score.expectedAttemptsNumerator / score.expectedAttemptsDenominator +
            (score.expectedAttemptsNumerator % score.expectedAttemptsDenominator != 0 ? 1u : 0u))
            errors.push_back("score_counter_ceiling");
        Score total = 0;
        for (Score const component : {evaluation.score.upgradeUtility, evaluation.score.usefulLife,
                 evaluation.score.profession, evaluation.score.acquisitionTime, evaluation.score.money,
                 evaluation.score.risk, evaluation.score.contention})
        {
            Score next = 0;
            if (!TryAdd(total, component, next))
            {
                errors.push_back("score_component_overflow:" + evaluation.stableId);
                break;
            }
            total = next;
        }
        if (total != evaluation.score.total)
            errors.push_back("score_total_mismatch:" + evaluation.stableId);
    }
    if ((allSelected && pursueCount != 1) || (!allSelected && pursueCount != 0))
        errors.push_back("pursue_count_mismatch");
    if (!plan.errors.empty() && allSelected)
        errors.push_back("error_plan_has_selection");
    try { json_contract::Validate(json_contract::Parse(EncodePlan(plan)), "gear-plan-v1.schema.json"); }
    catch (std::exception const&) { errors.push_back("gear_plan_schema"); }
    return errors;
}

static std::string EncodePlan(GearPlan const& plan)
{
    json::object root;
    root["schema_version"] = "cpi-207-gear-plan-v1";
    root["plan_id"] = plan.planId;
    root["character_id"] = plan.characterId;
    root["content_revision"] = plan.contentRevision;
    root["content_graph_digest"] = plan.contentGraphDigest;
    root["policy_version"] = plan.policyVersion;
    root["policy_identity"] = plan.policyIdentity;
    root["created_at_seconds"] = plan.createdAtSeconds;
    root["selected_target_id"] = OptionalString(plan.selectedTargetId);
    root["selected_source_id"] = OptionalString(plan.selectedSourceId);
    root["selected_item_id"] = OptionalUint32(plan.selectedItemId);

    json::array evaluations;
    evaluations.reserve(plan.evaluations.size());
    for (GearSourceEvaluation const& evaluation : plan.evaluations)
    {
        json::object score;
        score["upgrade_utility"] = evaluation.score.upgradeUtility;
        score["useful_life"] = evaluation.score.usefulLife;
        score["profession"] = evaluation.score.profession;
        score["acquisition_time"] = evaluation.score.acquisitionTime;
        score["money"] = evaluation.score.money;
        score["risk"] = evaluation.score.risk;
        score["contention"] = evaluation.score.contention;
        score["total"] = evaluation.score.total;
        score["expected_attempts_numerator"] = evaluation.score.expectedAttemptsNumerator;
        score["expected_attempts_denominator"] = evaluation.score.expectedAttemptsDenominator;
        score["expected_attempts_ceiling"] = evaluation.score.expectedAttemptsCeiling;
        score["expected_acquisition_seconds"] = evaluation.score.expectedAcquisitionSeconds;

        json::object serialized;
        serialized["target_id"] = evaluation.targetId;
        serialized["source_id"] = evaluation.stableId;
        serialized["item_id"] = evaluation.itemId;
        serialized["eligible"] = evaluation.eligible;
        serialized["disposition"] = DispositionName(evaluation.disposition);
        serialized["reason_codes"] = StringArray(evaluation.reasonCodes);
        serialized["score"] = std::move(score);
        evaluations.emplace_back(std::move(serialized));
    }
    root["evaluations"] = std::move(evaluations);

    json::array targets;
    targets.reserve(plan.targets.size());
    for (GearTargetEvaluation const& target : plan.targets)
    {
        json::object serialized;
        serialized["target_id"] = target.stableId;
        serialized["slot"] = target.slot;
        serialized["item_id"] = target.itemId;
        serialized["playerbots_upgrade_score"] = target.playerbotsUpgradeScore;
        serialized["expected_replacement_level"] = target.expectedReplacementLevel;
        serialized["profession_value"] = target.professionValue;
        serialized["status"] = TargetStatusName(target.status);
        serialized["reason_codes"] = StringArray(target.reasonCodes);
        targets.emplace_back(std::move(serialized));
    }
    root["targets"] = std::move(targets);
    root["errors"] = StringArray(plan.errors);
    return json::serialize(root);
}

std::string GearPlanToJson(GearPlan const& plan)
{
    auto errors = ValidateGearPlanForPersistence(plan);
    if (!errors.empty()) throw std::invalid_argument("gear_plan:invalid:" + errors.front());
    return EncodePlan(plan);
}

GearPlan GearPlanFromJson(std::string_view bytes)
{
    auto root = json_contract::Parse(bytes);
    json_contract::Validate(root, "gear-plan-v1.schema.json");
    auto text = [](json::value const& v) { return std::string(v.as_string()); };
    auto u32 = [](json::value const& v) {
        auto n = json_contract::Unsigned(v);
        if (n > std::numeric_limits<std::uint32_t>::max()) throw std::invalid_argument("gear_plan:uint32_overflow");
        return static_cast<std::uint32_t>(n);
    };
    auto strings = [&](json::value const& v) { std::vector<std::string> r; for (auto const& x : v.as_array()) r.push_back(text(x)); return r; };
    GearPlan p;
    p.planId=text(root.at("plan_id")); p.characterId=json_contract::Unsigned(root.at("character_id"));
    p.contentRevision=text(root.at("content_revision")); p.contentGraphDigest=text(root.at("content_graph_digest"));
    p.policyVersion=text(root.at("policy_version")); p.policyIdentity=text(root.at("policy_identity"));
    p.createdAtSeconds=json_contract::Signed(root.at("created_at_seconds"));
    if(!root.at("selected_target_id").is_null())p.selectedTargetId=text(root.at("selected_target_id"));
    if(!root.at("selected_source_id").is_null())p.selectedSourceId=text(root.at("selected_source_id"));
    if(!root.at("selected_item_id").is_null())p.selectedItemId=u32(root.at("selected_item_id"));
    p.errors=strings(root.at("errors"));
    for(auto const& v:root.at("targets").as_array()) {
        GearTargetEvaluation t;
        t.stableId=text(v.at("target_id")); t.slot=u32(v.at("slot")); t.itemId=u32(v.at("item_id"));
        t.playerbotsUpgradeScore=json_contract::Signed(v.at("playerbots_upgrade_score"));
        t.expectedReplacementLevel=u32(v.at("expected_replacement_level")); t.professionValue=json_contract::Signed(v.at("profession_value"));
        auto status=text(v.at("status"));
        t.status=status=="candidate"?GearTargetStatus::Candidate:status=="invalid"?GearTargetStatus::Invalid:GearTargetStatus::Unavailable;
        t.reasonCodes=strings(v.at("reason_codes")); p.targets.push_back(std::move(t));
    }
    for(auto const& v:root.at("evaluations").as_array()) {
        GearSourceEvaluation e;
        e.targetId=text(v.at("target_id")); e.stableId=text(v.at("source_id")); e.itemId=u32(v.at("item_id")); e.eligible=v.at("eligible").as_bool();
        auto disposition=text(v.at("disposition"));
        e.disposition=disposition=="pursue"?GearDisposition::Pursue:disposition=="deferred"?GearDisposition::Deferred:GearDisposition::Abandoned;
        e.reasonCodes=strings(v.at("reason_codes")); auto const& vscore=v.at("score"); auto& score=e.score;
        score.upgradeUtility=json_contract::Signed(vscore.at("upgrade_utility"));
        score.usefulLife=json_contract::Signed(vscore.at("useful_life")); score.profession=json_contract::Signed(vscore.at("profession"));
        score.acquisitionTime=json_contract::Signed(vscore.at("acquisition_time")); score.money=json_contract::Signed(vscore.at("money"));
        score.risk=json_contract::Signed(vscore.at("risk")); score.contention=json_contract::Signed(vscore.at("contention"));
        score.total=json_contract::Signed(vscore.at("total")); score.expectedAcquisitionSeconds=json_contract::Signed(vscore.at("expected_acquisition_seconds"));
        score.expectedAttemptsNumerator=json_contract::Unsigned(vscore.at("expected_attempts_numerator"));
        score.expectedAttemptsDenominator=json_contract::Unsigned(vscore.at("expected_attempts_denominator"));
        score.expectedAttemptsCeiling=json_contract::Unsigned(vscore.at("expected_attempts_ceiling"));
        p.evaluations.push_back(std::move(e));
    }
    auto errors=ValidateGearPlanForPersistence(p);
    if(!errors.empty())throw std::invalid_argument("gear_plan:invalid:"+errors.front());
    return p;
}
}
