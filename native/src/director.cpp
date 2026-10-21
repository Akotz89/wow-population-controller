#include "pbguide/director.hpp"

#include <algorithm>
#include <cctype>
#include <limits>
#include <set>
#include <stdexcept>
#include <utility>

namespace pbguide
{
namespace
{
void Reject(CandidateEvaluation& evaluation, std::string reason)
{
    if (std::find(evaluation.rejectionReasons.begin(), evaluation.rejectionReasons.end(), reason) ==
        evaluation.rejectionReasons.end())
        evaluation.rejectionReasons.push_back(std::move(reason));
}

bool MatchesFailureContext(BotProgressSnapshot const& snapshot, FailureEvidence const& failure)
{
    bool const scopeMatches = failure.scope == EvidenceScope::Global ||
        (failure.scope == EvidenceScope::Character && snapshot.characterId == failure.characterId &&
            snapshot.profileId == failure.profileId) ||
        (failure.scope == EvidenceScope::Cohort && snapshot.profileId == failure.profileId);
    return scopeMatches && snapshot.coreRevision == failure.coreRevision &&
        snapshot.playerbotsRevision == failure.playerbotsRevision &&
        snapshot.contentRevision == failure.contentRevision &&
        snapshot.contentGraphDigest == failure.contentGraphDigest && snapshot.phase == failure.phase &&
        snapshot.mapId == failure.mapId && snapshot.zoneId == failure.zoneId &&
        snapshot.groupSize == failure.groupSize &&
        snapshot.capabilityFingerprint == failure.capabilityFingerprint;
}

bool IsValidKind(ObjectiveKind kind)
{
    switch (kind)
    {
        case ObjectiveKind::Quest:
        case ObjectiveKind::Grind:
        case ObjectiveKind::Train:
        case ObjectiveKind::Vendor:
        case ObjectiveKind::SetHearth:
        case ObjectiveKind::Flight:
        case ObjectiveKind::ItemSource:
        case ObjectiveKind::Dungeon:
        case ObjectiveKind::Profession:
        case ObjectiveKind::Idle:
            return true;
    }
    return false;
}

bool IsValidScope(EvidenceScope scope)
{
    return scope == EvidenceScope::Character || scope == EvidenceScope::Cohort || scope == EvidenceScope::Global;
}

bool IsValidStableId(std::string const& stableId);
bool IsLowerHexDigest(std::string const& value);

bool IsValidFailureEvidence(FailureEvidence const& failure)
{
    return IsValidStableId(failure.stableId) && IsValidScope(failure.scope) &&
        failure.failureCount > 0 && failure.characterId != 0 && failure.phase != 0 &&
        failure.groupSize != 0 && failure.lastFailureSeconds >= 0 && !failure.classification.empty() &&
        !failure.coreRevision.empty() && !failure.playerbotsRevision.empty() &&
        !failure.contentRevision.empty() && IsLowerHexDigest(failure.contentGraphDigest) &&
        !failure.capabilityFingerprint.empty() &&
        (failure.scope == EvidenceScope::Global || !failure.profileId.empty());
}

bool IsValidStableId(std::string const& stableId)
{
    std::size_t const separator = stableId.find(':');
    if (separator == std::string::npos || separator == 0 || separator + 1 >= stableId.size() ||
        !std::islower(static_cast<unsigned char>(stableId.front())))
        return false;

    return std::all_of(stableId.begin(), stableId.end(), [](char value) {
        unsigned char const character = static_cast<unsigned char>(value);
        return std::isalnum(character) || value == '_' || value == '-' || value == '.' || value == ':';
    });
}

bool IsLowerHexDigest(std::string const& value)
{
    return value.size() == 64 && std::all_of(value.begin(), value.end(), [](char character) {
        unsigned char const byte = static_cast<unsigned char>(character);
        return std::isdigit(byte) || (character >= 'a' && character <= 'f');
    });
}

bool HasInvalidCandidateMetrics(ObjectiveCandidate const& candidate)
{
    return candidate.xpValue < 0 || candidate.unlockValue < 0 || candidate.gearValue < 0 ||
        candidate.professionValue < 0 || candidate.continuityValue < 0 || candidate.groupSynergyValue < 0 ||
        candidate.travelSeconds < 0 || candidate.executionSeconds < 0 || candidate.riskMilli < 0 ||
        candidate.contentionMilli < 0;
}

bool IsValidSnapshotInternal(BotProgressSnapshot const& snapshot) noexcept
{
    return snapshot.characterId != 0 && snapshot.level != 0 && snapshot.level <= 80 && snapshot.phase != 0 &&
        snapshot.groupSize != 0 && snapshot.groupSize <= 40 && snapshot.nowSeconds >= 0 &&
        !snapshot.coreRevision.empty() && !snapshot.playerbotsRevision.empty() &&
        !snapshot.contentRevision.empty() && !snapshot.contentGraphDigest.empty() && !snapshot.routeId.empty() &&
        !snapshot.routeRevision.empty() && !snapshot.catalogueDigest.empty() && !snapshot.profileId.empty() &&
        !snapshot.profileVersion.empty() && !snapshot.capabilityFingerprint.empty();
}

bool IsValidSnapshotIdentityInternal(BotProgressSnapshot const& snapshot) noexcept
{
    return IsValidStableId(snapshot.routeId) && IsValidStableId(snapshot.profileId) &&
        IsLowerHexDigest(snapshot.catalogueDigest) && IsLowerHexDigest(snapshot.contentGraphDigest);
}

bool IsGuideControlSuspendedInternal(BotProgressSnapshot const& snapshot) noexcept
{
    return snapshot.inCombat || snapshot.onTaxi || snapshot.inBattlegroundOrArena || snapshot.loggingOut ||
        snapshot.forcedTravel || snapshot.hasHumanMaster;
}

bool TryMultiplyNonNegative(Score left, Score right, Score& result)
{
    if (left < 0 || right < 0)
        return false;
    if (left != 0 && right > std::numeric_limits<Score>::max() / left)
        return false;
    result = left * right;
    return true;
}

bool TryAdd(Score left, Score right, Score& result)
{
    if ((right > 0 && left > std::numeric_limits<Score>::max() - right) ||
        (right < 0 && left < std::numeric_limits<Score>::min() - right))
        return false;
    result = left + right;
    return true;
}

bool TrySubtractProduct(Score value, Score weight, Score& result)
{
    Score product = 0;
    if (!TryMultiplyNonNegative(value, weight, product))
        return false;
    result = -product;
    return true;
}

bool IsDefaultV1Policy(ScoringPolicy const& policy)
{
    ScoringPolicy const expected;
    return policy.xpWeight == expected.xpWeight && policy.unlockWeight == expected.unlockWeight &&
        policy.gearWeight == expected.gearWeight && policy.professionWeight == expected.professionWeight &&
        policy.continuityWeight == expected.continuityWeight &&
        policy.groupSynergyWeight == expected.groupSynergyWeight &&
        policy.travelSecondCost == expected.travelSecondCost &&
        policy.executionSecondCost == expected.executionSecondCost &&
        policy.riskMilliDivisor == expected.riskMilliDivisor &&
        policy.contentionMilliDivisor == expected.contentionMilliDivisor &&
        policy.priorFailureCost == expected.priorFailureCost &&
        policy.baseFailureBackoffSeconds == expected.baseFailureBackoffSeconds &&
        policy.maximumBackoffDoublings == expected.maximumBackoffDoublings &&
        policy.failureDecaySeconds == expected.failureDecaySeconds;
}
}

bool IsValidBotProgressSnapshot(BotProgressSnapshot const& snapshot) noexcept
{
    return IsValidSnapshotInternal(snapshot);
}

bool IsValidBotProgressSnapshotIdentity(BotProgressSnapshot const& snapshot) noexcept
{
    return IsValidSnapshotIdentityInternal(snapshot);
}

bool IsGuideControlSuspended(BotProgressSnapshot const& snapshot) noexcept
{
    return IsGuideControlSuspendedInternal(snapshot);
}

std::string CanonicalPolicyIdentity(ScoringPolicy const& policy)
{
    return policy.version + "|xp=" + std::to_string(policy.xpWeight) +
        "|unlock=" + std::to_string(policy.unlockWeight) + "|gear=" + std::to_string(policy.gearWeight) +
        "|profession=" + std::to_string(policy.professionWeight) +
        "|continuity=" + std::to_string(policy.continuityWeight) +
        "|group=" + std::to_string(policy.groupSynergyWeight) +
        "|travel=" + std::to_string(policy.travelSecondCost) +
        "|execution=" + std::to_string(policy.executionSecondCost) +
        "|risk_div=" + std::to_string(policy.riskMilliDivisor) +
        "|contention_div=" + std::to_string(policy.contentionMilliDivisor) +
        "|failure=" + std::to_string(policy.priorFailureCost) +
        "|backoff=" + std::to_string(policy.baseFailureBackoffSeconds) +
        "|doublings=" + std::to_string(policy.maximumBackoffDoublings) +
        "|decay=" + std::to_string(policy.failureDecaySeconds);
}

StrategicGuideDirector::StrategicGuideDirector(ScoringPolicy policy) : policy_(std::move(policy))
{
    if (policy_.version.empty())
        throw std::invalid_argument("scoring policy version must not be empty");
    if (policy_.xpWeight < 0 || policy_.unlockWeight < 0 || policy_.gearWeight < 0 ||
        policy_.professionWeight < 0 || policy_.continuityWeight < 0 || policy_.groupSynergyWeight < 0 ||
        policy_.travelSecondCost < 0 || policy_.executionSecondCost < 0 || policy_.priorFailureCost < 0)
        throw std::invalid_argument("scoring weights and costs must not be negative");
    if (policy_.riskMilliDivisor <= 0 || policy_.contentionMilliDivisor <= 0)
        throw std::invalid_argument("scoring divisors must be positive");
    if (policy_.baseFailureBackoffSeconds < 0)
        throw std::invalid_argument("failure backoff must not be negative");
    if (policy_.failureDecaySeconds <= 0)
        throw std::invalid_argument("failure decay interval must be positive");
    if (policy_.maximumBackoffDoublings >= 63 ||
        policy_.baseFailureBackoffSeconds >
            (std::numeric_limits<std::int64_t>::max() >> policy_.maximumBackoffDoublings))
        throw std::invalid_argument("failure backoff policy would overflow");
    if (policy_.version == "cpi-207-v1" && !IsDefaultV1Policy(policy_))
        throw std::invalid_argument("reserved policy version cpi-207-v1 has noncanonical constants");
}

CandidateEvaluation StrategicGuideDirector::Evaluate(
    BotProgressSnapshot const& snapshot,
    ObjectiveCandidate const& candidate,
    FailureEvidence const* failure) const
{
    CandidateEvaluation evaluation;
    evaluation.stableId = candidate.stableId;

    if (!IsValidStableId(candidate.stableId))
        Reject(evaluation, "invalid_stable_id");
    if (!IsValidBotProgressSnapshot(snapshot))
        Reject(evaluation, "invalid_snapshot");
    if (!IsValidBotProgressSnapshotIdentity(snapshot))
        Reject(evaluation, "invalid_snapshot_identity");
    if (candidate.minimumLevel == 0 || candidate.maximumLevel == 0 ||
        candidate.minimumLevel > candidate.maximumLevel || candidate.minimumPhase == 0 ||
        candidate.maximumPhase == 0 || candidate.minimumPhase > candidate.maximumPhase ||
        candidate.requiredGroupSize == 0 || !IsValidKind(candidate.kind) || HasInvalidCandidateMetrics(candidate))
        Reject(evaluation, "invalid_candidate_data");
    if (!snapshot.populationAdmitted)
        Reject(evaluation, "population_not_admitted");
    if (!snapshot.scheduleActive)
        Reject(evaluation, "schedule_inactive");
    if (IsGuideControlSuspended(snapshot))
        Reject(evaluation, "guide_control_suspended");
    if (!candidate.available)
        Reject(evaluation, "objective_unavailable");
    if (!candidate.phaseAuthorized)
        Reject(evaluation, "phase_not_authorized");
    if (snapshot.level < candidate.minimumLevel || snapshot.level > candidate.maximumLevel)
        Reject(evaluation, "level_out_of_range");
    if (snapshot.phase < candidate.minimumPhase || snapshot.phase > candidate.maximumPhase)
        Reject(evaluation, "phase_out_of_range");
    if (snapshot.groupSize < candidate.requiredGroupSize)
        Reject(evaluation, "party_not_ready");
    if (!candidate.earnedSource)
        Reject(evaluation, "unearned_or_synthetic_source");

    std::set<std::string> seenCapabilities;
    for (std::string const& capability : candidate.requiredCapabilities)
    {
        if (capability.empty() || !seenCapabilities.insert(capability).second)
            Reject(evaluation, "invalid_required_capability");
    }
    for (std::string const& capability : seenCapabilities)
    {
        auto const found = snapshot.capabilities.find(capability);
        if (found == snapshot.capabilities.end() || !found->second)
            Reject(evaluation, "missing_capability:" + capability);
    }

    std::uint32_t priorFailureCount = 0;
    if (failure != nullptr && failure->stableId != candidate.stableId)
        Reject(evaluation, "failure_objective_mismatch");
    if (failure != nullptr && failure->stableId == candidate.stableId && !IsValidFailureEvidence(*failure))
        Reject(evaluation, "invalid_failure_evidence");
    if (failure != nullptr && failure->stableId == candidate.stableId &&
        failure->lastFailureSeconds > snapshot.nowSeconds)
        Reject(evaluation, "invalid_failure_evidence");
    if (failure != nullptr && failure->stableId == candidate.stableId && failure->failureCount > 0 &&
        MatchesFailureContext(snapshot, *failure) && evaluation.rejectionReasons.empty())
    {
        std::int64_t const elapsed = snapshot.nowSeconds - failure->lastFailureSeconds;
        std::uint64_t const decaySteps =
            static_cast<std::uint64_t>(elapsed / policy_.failureDecaySeconds);
        if (decaySteps < failure->failureCount)
            priorFailureCount = failure->failureCount - static_cast<std::uint32_t>(decaySteps);
        if (priorFailureCount > 0)
        {
            std::int64_t const retryDelay = RetryDelaySeconds(priorFailureCount);
            if (!TryAdd(failure->lastFailureSeconds, retryDelay, evaluation.retryAtSeconds))
                Reject(evaluation, "failure_time_overflow");
            else if (snapshot.nowSeconds < evaluation.retryAtSeconds)
                Reject(evaluation, "failure_backoff_active");
        }
    }

    evaluation.eligible = evaluation.rejectionReasons.empty();
    if (!evaluation.eligible)
        return evaluation;

    ScoreBreakdown& score = evaluation.score;
    bool validScore = TryMultiplyNonNegative(candidate.xpValue, policy_.xpWeight, score.xp) &&
        TryMultiplyNonNegative(candidate.unlockValue, policy_.unlockWeight, score.unlocks) &&
        TryMultiplyNonNegative(candidate.gearValue, policy_.gearWeight, score.gear) &&
        TryMultiplyNonNegative(candidate.professionValue, policy_.professionWeight, score.profession) &&
        TryMultiplyNonNegative(candidate.continuityValue, policy_.continuityWeight, score.continuity) &&
        TryMultiplyNonNegative(candidate.groupSynergyValue, policy_.groupSynergyWeight, score.groupSynergy) &&
        TrySubtractProduct(candidate.travelSeconds, policy_.travelSecondCost, score.travel) &&
        TrySubtractProduct(candidate.executionSeconds, policy_.executionSecondCost, score.execution);
    score.risk = -(candidate.riskMilli / policy_.riskMilliDivisor);
    score.contention = -(candidate.contentionMilli / policy_.contentionMilliDivisor);
    validScore = validScore &&
        TrySubtractProduct(static_cast<Score>(priorFailureCount), policy_.priorFailureCost, score.priorFailures);

    Score total = 0;
    for (Score const component : {score.xp, score.unlocks, score.gear, score.profession, score.continuity,
             score.groupSynergy, score.travel, score.execution, score.risk, score.contention, score.priorFailures})
    {
        Score next = 0;
        if (!validScore || !TryAdd(total, component, next))
        {
            validScore = false;
            break;
        }
        total = next;
    }
    if (!validScore)
    {
        evaluation.eligible = false;
        Reject(evaluation, "score_overflow");
        return evaluation;
    }
    score.total = total;
    return evaluation;
}

PlanDecision StrategicGuideDirector::Select(
    BotProgressSnapshot const& snapshot,
    std::vector<ObjectiveCandidate> const& candidates,
    std::vector<FailureEvidence> const& failures) const
{
    PlanDecision decision;
    decision.policyVersion = policy_.version;
    decision.policyIdentity = CanonicalPolicyIdentity(policy_);
    decision.evaluations.reserve(candidates.size());

    std::map<std::string, std::size_t> candidateCounts;
    for (ObjectiveCandidate const& candidate : candidates)
        ++candidateCounts[candidate.stableId];

    std::map<std::string, FailureEvidence const*> failureById;
    std::map<std::string, FailureEvidence const*> invalidFailureById;
    for (FailureEvidence const& failure : failures)
    {
        if (!IsValidFailureEvidence(failure))
        {
            if (candidateCounts.contains(failure.stableId))
                invalidFailureById.emplace(failure.stableId, &failure);
            continue;
        }
        if (!MatchesFailureContext(snapshot, failure))
            continue;
        auto const found = failureById.find(failure.stableId);
        if (found == failureById.end() || found->second->lastFailureSeconds < failure.lastFailureSeconds ||
            (found->second->lastFailureSeconds == failure.lastFailureSeconds &&
                found->second->failureCount < failure.failureCount))
            failureById[failure.stableId] = &failure;
    }

    for (ObjectiveCandidate const& candidate : candidates)
    {
        FailureEvidence const* failure = nullptr;
        auto const invalidFound = invalidFailureById.find(candidate.stableId);
        if (invalidFound != invalidFailureById.end())
            failure = invalidFound->second;
        else
        {
            auto const found = failureById.find(candidate.stableId);
            if (found != failureById.end())
                failure = found->second;
        }
        CandidateEvaluation evaluation = Evaluate(snapshot, candidate, failure);
        if (candidateCounts[candidate.stableId] > 1)
        {
            evaluation.eligible = false;
            Reject(evaluation, "duplicate_candidate_id");
        }
        decision.evaluations.push_back(std::move(evaluation));
    }

    std::sort(decision.evaluations.begin(), decision.evaluations.end(), [](CandidateEvaluation const& left,
                                                                             CandidateEvaluation const& right) {
        if (left.eligible != right.eligible)
            return left.eligible > right.eligible;
        if (left.eligible && left.score.total != right.score.total)
            return left.score.total > right.score.total;
        return left.stableId < right.stableId;
    });

    if (!decision.evaluations.empty() && decision.evaluations.front().eligible)
    {
        decision.selectedStableId = decision.evaluations.front().stableId;
        decision.disposition = PlanDisposition::SelectedObjective;
    }
    else if (IsValidBotProgressSnapshot(snapshot) && IsValidBotProgressSnapshotIdentity(snapshot) &&
        snapshot.populationAdmitted && snapshot.scheduleActive && !IsGuideControlSuspended(snapshot))
    {
        decision.disposition = PlanDisposition::StockNewRpgFallback;
    }
    return decision;
}

ScoringPolicy const& StrategicGuideDirector::Policy() const noexcept
{
    return policy_;
}

std::int64_t StrategicGuideDirector::RetryDelaySeconds(std::uint32_t failureCount) const
{
    if (failureCount == 0)
        return 0;
    std::uint32_t const requestedDoublings = failureCount - 1;
    std::uint32_t const doublings = std::min(requestedDoublings, policy_.maximumBackoffDoublings);
    return policy_.baseFailureBackoffSeconds * (static_cast<std::int64_t>(1) << doublings);
}
}
