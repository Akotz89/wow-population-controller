#include "pbguide/replay.hpp"
#include "json_contract.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <cctype>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>

namespace pbguide
{
namespace
{
namespace json = boost::json;

json::object const& ObjectAt(json::object const& object, std::string_view key)
{
    return object.at(key).as_object();
}

json::array const& ArrayAt(json::object const& object, std::string_view key)
{
    return object.at(key).as_array();
}

std::string StringAt(json::object const& object, std::string_view key)
{
    json::string const& value = object.at(key).as_string();
    return std::string(value.data(), value.size());
}

bool BoolAt(json::object const& object, std::string_view key)
{
    return object.at(key).as_bool();
}

std::int64_t Int64At(json::object const& object, std::string_view key)
{
    json::value const& value = object.at(key);
    if (value.is_int64())
        return value.as_int64();
    std::uint64_t const unsignedValue = value.as_uint64();
    if (unsignedValue > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
        throw std::invalid_argument("integer is outside int64 range: " + std::string(key));
    return static_cast<std::int64_t>(unsignedValue);
}

std::uint32_t Uint32At(json::object const& object, std::string_view key)
{
    std::int64_t const value = Int64At(object, key);
    if (value < 0 || static_cast<std::uint64_t>(value) > std::numeric_limits<std::uint32_t>::max())
        throw std::invalid_argument("integer is outside uint32 range: " + std::string(key));
    return static_cast<std::uint32_t>(value);
}

std::uint64_t Uint64At(json::object const& object, std::string_view key)
{
    json::value const& value = object.at(key);
    if (value.is_uint64())
        return value.as_uint64();
    std::int64_t const signedValue = value.as_int64();
    if (signedValue < 0)
        throw std::invalid_argument("integer is outside uint64 range: " + std::string(key));
    return static_cast<std::uint64_t>(signedValue);
}

ObjectiveKind ParseKind(std::string const& kind)
{
    if (kind == "quest")
        return ObjectiveKind::Quest;
    if (kind == "grind")
        return ObjectiveKind::Grind;
    if (kind == "train")
        return ObjectiveKind::Train;
    if (kind == "vendor")
        return ObjectiveKind::Vendor;
    if (kind == "set_hearth")
        return ObjectiveKind::SetHearth;
    if (kind == "flight")
        return ObjectiveKind::Flight;
    if (kind == "item_source")
        return ObjectiveKind::ItemSource;
    if (kind == "dungeon")
        return ObjectiveKind::Dungeon;
    if (kind == "profession")
        return ObjectiveKind::Profession;
    if (kind == "idle")
        return ObjectiveKind::Idle;
    throw std::invalid_argument("unknown objective kind: " + kind);
}

EvidenceScope ParseScope(std::string const& scope)
{
    if (scope == "character")
        return EvidenceScope::Character;
    if (scope == "cohort")
        return EvidenceScope::Cohort;
    if (scope == "global")
        return EvidenceScope::Global;
    throw std::invalid_argument("unknown failure scope: " + scope);
}

PlanDisposition ParseDisposition(std::string const& disposition)
{
    if (disposition == "selected_objective")
        return PlanDisposition::SelectedObjective;
    if (disposition == "stock_new_rpg_fallback")
        return PlanDisposition::StockNewRpgFallback;
    if (disposition == "defer_existing_control")
        return PlanDisposition::DeferExistingControl;
    throw std::invalid_argument("unknown plan disposition: " + disposition);
}

std::string DispositionName(PlanDisposition disposition)
{
    switch (disposition)
    {
        case PlanDisposition::SelectedObjective:
            return "selected_objective";
        case PlanDisposition::StockNewRpgFallback:
            return "stock_new_rpg_fallback";
        case PlanDisposition::DeferExistingControl:
            return "defer_existing_control";
    }
    throw std::invalid_argument("invalid plan disposition");
}

bool IsLowerHexDigest(std::string const& value)
{
    return value.size() == 64 && std::all_of(value.begin(), value.end(), [](char character) {
        unsigned char const byte = static_cast<unsigned char>(character);
        return std::isdigit(byte) || (character >= 'a' && character <= 'f');
    });
}

std::vector<std::string> ParseStringArray(json::array const& values)
{
    std::vector<std::string> result;
    result.reserve(values.size());
    for (json::value const& value : values)
    {
        json::string const& text = value.as_string();
        result.emplace_back(text.data(), text.size());
    }
    return result;
}

ScoringPolicy ParsePolicy(json::object const& object)
{
    ScoringPolicy policy;
    policy.version = StringAt(object, "version");
    policy.xpWeight = Int64At(object, "xp_weight");
    policy.unlockWeight = Int64At(object, "unlock_weight");
    policy.gearWeight = Int64At(object, "gear_weight");
    policy.professionWeight = Int64At(object, "profession_weight");
    policy.continuityWeight = Int64At(object, "continuity_weight");
    policy.groupSynergyWeight = Int64At(object, "group_synergy_weight");
    policy.travelSecondCost = Int64At(object, "travel_second_cost");
    policy.executionSecondCost = Int64At(object, "execution_second_cost");
    policy.riskMilliDivisor = Int64At(object, "risk_milli_divisor");
    policy.contentionMilliDivisor = Int64At(object, "contention_milli_divisor");
    policy.priorFailureCost = Int64At(object, "prior_failure_cost");
    policy.baseFailureBackoffSeconds = Int64At(object, "base_failure_backoff_seconds");
    policy.maximumBackoffDoublings = Uint32At(object, "maximum_backoff_doublings");
    policy.failureDecaySeconds = Int64At(object, "failure_decay_seconds");
    return policy;
}

BotProgressSnapshot ParseSnapshot(json::object const& object)
{
    BotProgressSnapshot snapshot;
    snapshot.characterId = Uint64At(object, "character_id");
    snapshot.level = Uint32At(object, "level");
    snapshot.phase = Uint32At(object, "phase");
    snapshot.groupSize = Uint32At(object, "group_size");
    snapshot.mapId = Uint32At(object, "map_id");
    snapshot.zoneId = Uint32At(object, "zone_id");
    snapshot.nowSeconds = Int64At(object, "now_seconds");
    snapshot.coreRevision = StringAt(object, "core_revision");
    snapshot.playerbotsRevision = StringAt(object, "playerbots_revision");
    snapshot.contentRevision = StringAt(object, "content_revision");
    snapshot.contentGraphDigest = StringAt(object, "content_graph_digest");
    snapshot.routeId = StringAt(object, "route_id");
    snapshot.routeRevision = StringAt(object, "route_revision");
    snapshot.catalogueDigest = StringAt(object, "catalogue_digest");
    snapshot.profileId = StringAt(object, "profile_id");
    snapshot.profileVersion = StringAt(object, "profile_version");
    snapshot.capabilityFingerprint = StringAt(object, "capability_fingerprint");
    snapshot.populationAdmitted = BoolAt(object, "population_admitted");
    snapshot.scheduleActive = BoolAt(object, "schedule_active");
    snapshot.inCombat = BoolAt(object, "in_combat");
    snapshot.onTaxi = BoolAt(object, "on_taxi");
    snapshot.inBattlegroundOrArena = BoolAt(object, "in_battleground_or_arena");
    snapshot.loggingOut = BoolAt(object, "logging_out");
    snapshot.forcedTravel = BoolAt(object, "forced_travel");
    snapshot.hasHumanMaster = BoolAt(object, "has_human_master");
    for (auto const& capability : ObjectAt(object, "capabilities"))
        snapshot.capabilities.emplace(
            std::string(capability.key().data(), capability.key().size()), capability.value().as_bool());
    return snapshot;
}

ObjectiveCandidate ParseCandidate(json::object const& object)
{
    ObjectiveCandidate candidate;
    candidate.stableId = StringAt(object, "stable_id");
    candidate.kind = ParseKind(StringAt(object, "kind"));
    candidate.minimumLevel = Uint32At(object, "minimum_level");
    candidate.maximumLevel = Uint32At(object, "maximum_level");
    candidate.minimumPhase = Uint32At(object, "minimum_phase");
    candidate.maximumPhase = Uint32At(object, "maximum_phase");
    candidate.requiredGroupSize = Uint32At(object, "required_group_size");
    candidate.available = BoolAt(object, "available");
    candidate.phaseAuthorized = BoolAt(object, "phase_authorized");
    candidate.earnedSource = BoolAt(object, "earned_source");
    candidate.requiredCapabilities = ParseStringArray(ArrayAt(object, "required_capabilities"));
    candidate.xpValue = Int64At(object, "xp_value");
    candidate.unlockValue = Int64At(object, "unlock_value");
    candidate.gearValue = Int64At(object, "gear_value");
    candidate.professionValue = Int64At(object, "profession_value");
    candidate.continuityValue = Int64At(object, "continuity_value");
    candidate.groupSynergyValue = Int64At(object, "group_synergy_value");
    candidate.travelSeconds = Int64At(object, "travel_seconds");
    candidate.executionSeconds = Int64At(object, "execution_seconds");
    candidate.riskMilli = Int64At(object, "risk_milli");
    candidate.contentionMilli = Int64At(object, "contention_milli");
    return candidate;
}

FailureEvidence ParseFailure(json::object const& object)
{
    FailureEvidence failure;
    failure.stableId = StringAt(object, "stable_id");
    failure.scope = ParseScope(StringAt(object, "scope"));
    failure.characterId = Uint64At(object, "character_id");
    failure.phase = Uint32At(object, "phase");
    failure.mapId = Uint32At(object, "map_id");
    failure.zoneId = Uint32At(object, "zone_id");
    failure.groupSize = Uint32At(object, "group_size");
    failure.failureCount = Uint32At(object, "failure_count");
    failure.lastFailureSeconds = Int64At(object, "last_failure_seconds");
    failure.classification = StringAt(object, "classification");
    failure.coreRevision = StringAt(object, "core_revision");
    failure.playerbotsRevision = StringAt(object, "playerbots_revision");
    failure.contentRevision = StringAt(object, "content_revision");
    failure.contentGraphDigest = StringAt(object, "content_graph_digest");
    failure.profileId = StringAt(object, "profile_id");
    failure.capabilityFingerprint = StringAt(object, "capability_fingerprint");
    return failure;
}

ExpectedEvaluation ParseExpectedEvaluation(json::object const& object)
{
    ExpectedEvaluation expected;
    expected.stableId = StringAt(object, "stable_id");
    expected.eligible = BoolAt(object, "eligible");
    json::object const& score = ObjectAt(object, "score");
    expected.score.xp = Int64At(score, "xp");
    expected.score.unlocks = Int64At(score, "unlocks");
    expected.score.gear = Int64At(score, "gear");
    expected.score.profession = Int64At(score, "profession");
    expected.score.continuity = Int64At(score, "continuity");
    expected.score.groupSynergy = Int64At(score, "group_synergy");
    expected.score.travel = Int64At(score, "travel");
    expected.score.execution = Int64At(score, "execution");
    expected.score.risk = Int64At(score, "risk");
    expected.score.contention = Int64At(score, "contention");
    expected.score.priorFailures = Int64At(score, "prior_failures");
    expected.score.total = Int64At(score, "total");
    expected.retryAtSeconds = Int64At(object, "retry_at_seconds");
    expected.rejectionReasons = ParseStringArray(ArrayAt(object, "rejection_reasons"));
    return expected;
}
}

BotProgressSnapshot ParseGearSnapshot(std::string_view jsonText)
{
    return ParseSnapshot(json_contract::Parse(jsonText).as_object());
}

ReplayCase ParseReplayCase(std::string_view jsonText)
{
    try
    {
        json::value parsed = json::parse(jsonText);
        json::object const& root = parsed.as_object();
        ReplayCase replay;
        replay.schemaVersion = StringAt(root, "schema_version");
        replay.catalogueDigest = StringAt(root, "catalogue_digest");
        replay.policy = ParsePolicy(ObjectAt(root, "policy"));
        replay.snapshot = ParseSnapshot(ObjectAt(root, "snapshot"));
        json::array const& candidates = ArrayAt(root, "candidates");
        json::array const& failures = ArrayAt(root, "failures");
        if (candidates.empty() || candidates.size() > 10'000 || failures.size() > 10'000)
            throw std::invalid_argument("replay collection size is outside the supported range");
        for (json::value const& candidate : candidates)
            replay.candidates.push_back(ParseCandidate(candidate.as_object()));
        for (json::value const& failure : failures)
            replay.failures.push_back(ParseFailure(failure.as_object()));

        json::object const& expected = ObjectAt(root, "expected");
        replay.expected.disposition = ParseDisposition(StringAt(expected, "disposition"));
        json::value const& selected = expected.at("selected_stable_id");
        if (!selected.is_null())
        {
            json::string const& selectedText = selected.as_string();
            replay.expected.selectedStableId = std::string(selectedText.data(), selectedText.size());
        }
        json::array const& evaluations = ArrayAt(expected, "evaluations");
        if (evaluations.size() > 10'000)
            throw std::invalid_argument("expected evaluation count exceeds the supported range");
        for (json::value const& evaluation : evaluations)
            replay.expected.evaluations.push_back(ParseExpectedEvaluation(evaluation.as_object()));
        return replay;
    }
    catch (std::exception const& error)
    {
        throw std::invalid_argument(std::string("invalid replay envelope: ") + error.what());
    }
}

std::vector<std::string> VerifyReplay(ReplayCase const& replay)
{
    std::vector<std::string> errors;
    if (replay.schemaVersion != "cpi-207-replay-v1")
        errors.push_back("schema_version_mismatch");
    if (!IsLowerHexDigest(replay.catalogueDigest))
        errors.push_back("catalogue_digest_invalid");
    if (replay.snapshot.catalogueDigest != replay.catalogueDigest)
        errors.push_back("snapshot_catalogue_digest_mismatch");

    StrategicGuideDirector const director(replay.policy);
    PlanDecision const actual = director.Select(replay.snapshot, replay.candidates, replay.failures);
    if (actual.disposition != replay.expected.disposition)
        errors.push_back("disposition_mismatch");
    if (actual.selectedStableId != replay.expected.selectedStableId)
        errors.push_back("selected_stable_id_mismatch");
    if (actual.evaluations.size() != replay.expected.evaluations.size())
    {
        errors.push_back("evaluation_count_mismatch");
        return errors;
    }

    for (std::size_t index = 0; index < actual.evaluations.size(); ++index)
    {
        CandidateEvaluation const& actualEvaluation = actual.evaluations[index];
        ExpectedEvaluation const& expectedEvaluation = replay.expected.evaluations[index];
        std::string const prefix = "evaluation[" + std::to_string(index) + "]:";
        if (actualEvaluation.stableId != expectedEvaluation.stableId)
            errors.push_back(prefix + "stable_id_mismatch");
        if (actualEvaluation.eligible != expectedEvaluation.eligible)
            errors.push_back(prefix + "eligible_mismatch");
        if (actualEvaluation.score.xp != expectedEvaluation.score.xp ||
            actualEvaluation.score.unlocks != expectedEvaluation.score.unlocks ||
            actualEvaluation.score.gear != expectedEvaluation.score.gear ||
            actualEvaluation.score.profession != expectedEvaluation.score.profession ||
            actualEvaluation.score.continuity != expectedEvaluation.score.continuity ||
            actualEvaluation.score.groupSynergy != expectedEvaluation.score.groupSynergy ||
            actualEvaluation.score.travel != expectedEvaluation.score.travel ||
            actualEvaluation.score.execution != expectedEvaluation.score.execution ||
            actualEvaluation.score.risk != expectedEvaluation.score.risk ||
            actualEvaluation.score.contention != expectedEvaluation.score.contention ||
            actualEvaluation.score.priorFailures != expectedEvaluation.score.priorFailures ||
            actualEvaluation.score.total != expectedEvaluation.score.total)
            errors.push_back(prefix + "score_breakdown_mismatch");
        if (actualEvaluation.retryAtSeconds != expectedEvaluation.retryAtSeconds)
            errors.push_back(prefix + "retry_at_mismatch");
        if (actualEvaluation.rejectionReasons != expectedEvaluation.rejectionReasons)
            errors.push_back(prefix + "rejection_reasons_mismatch");
    }
    return errors;
}

std::string DecisionToJson(PlanDecision const& decision)
{
    json::object root;
    root["policy_version"] = decision.policyVersion;
    root["policy_identity"] = decision.policyIdentity;
    root["disposition"] = DispositionName(decision.disposition);
    if (decision.selectedStableId.has_value())
        root["selected_stable_id"] = *decision.selectedStableId;
    else
        root["selected_stable_id"] = nullptr;

    json::array evaluations;
    for (CandidateEvaluation const& evaluation : decision.evaluations)
    {
        json::object item;
        item["stable_id"] = evaluation.stableId;
        item["eligible"] = evaluation.eligible;
        json::object score;
        score["xp"] = evaluation.score.xp;
        score["unlocks"] = evaluation.score.unlocks;
        score["gear"] = evaluation.score.gear;
        score["profession"] = evaluation.score.profession;
        score["continuity"] = evaluation.score.continuity;
        score["group_synergy"] = evaluation.score.groupSynergy;
        score["travel"] = evaluation.score.travel;
        score["execution"] = evaluation.score.execution;
        score["risk"] = evaluation.score.risk;
        score["contention"] = evaluation.score.contention;
        score["prior_failures"] = evaluation.score.priorFailures;
        score["total"] = evaluation.score.total;
        item["score"] = std::move(score);
        item["retry_at_seconds"] = evaluation.retryAtSeconds;
        json::array reasons;
        for (std::string const& reason : evaluation.rejectionReasons)
            reasons.emplace_back(reason);
        item["rejection_reasons"] = std::move(reasons);
        evaluations.emplace_back(std::move(item));
    }
    root["evaluations"] = std::move(evaluations);
    return json::serialize(root);
}
}
