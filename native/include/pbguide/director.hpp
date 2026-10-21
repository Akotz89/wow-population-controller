#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace pbguide
{
using Score = std::int64_t;

enum class ObjectiveKind : std::uint8_t
{
    Quest,
    Grind,
    Train,
    Vendor,
    SetHearth,
    Flight,
    ItemSource,
    Dungeon,
    Profession,
    Idle
};

struct BotProgressSnapshot
{
    std::uint64_t characterId = 0;
    std::uint32_t level = 1;
    std::uint32_t phase = 1;
    std::uint32_t groupSize = 1;
    std::uint32_t mapId = 0;
    std::uint32_t zoneId = 0;
    std::int64_t nowSeconds = 0;
    std::string coreRevision;
    std::string playerbotsRevision;
    std::string contentRevision;
    std::string contentGraphDigest;
    std::string routeId;
    std::string routeRevision;
    std::string catalogueDigest;
    std::string profileId;
    std::string profileVersion;
    std::string capabilityFingerprint;
    bool populationAdmitted = false;
    bool scheduleActive = false;
    bool inCombat = false;
    bool onTaxi = false;
    bool inBattlegroundOrArena = false;
    bool loggingOut = false;
    bool forcedTravel = false;
    bool hasHumanMaster = false;
    std::map<std::string, bool> capabilities;
};

[[nodiscard]] bool IsValidBotProgressSnapshot(BotProgressSnapshot const& snapshot) noexcept;
[[nodiscard]] bool IsValidBotProgressSnapshotIdentity(BotProgressSnapshot const& snapshot) noexcept;
[[nodiscard]] bool IsGuideControlSuspended(BotProgressSnapshot const& snapshot) noexcept;

enum class EvidenceScope : std::uint8_t
{
    Character,
    Cohort,
    Global
};

struct ObjectiveCandidate
{
    std::string stableId;
    ObjectiveKind kind = ObjectiveKind::Idle;
    std::uint32_t minimumLevel = 1;
    std::uint32_t maximumLevel = 80;
    std::uint32_t minimumPhase = 1;
    std::uint32_t maximumPhase = 99;
    std::uint32_t requiredGroupSize = 1;
    bool available = true;
    bool phaseAuthorized = true;
    bool earnedSource = true;
    std::vector<std::string> requiredCapabilities;

    Score xpValue = 0;
    Score unlockValue = 0;
    Score gearValue = 0;
    Score professionValue = 0;
    Score continuityValue = 0;
    Score groupSynergyValue = 0;
    Score travelSeconds = 0;
    Score executionSeconds = 0;
    Score riskMilli = 0;
    Score contentionMilli = 0;
};

struct FailureEvidence
{
    std::string stableId;
    EvidenceScope scope = EvidenceScope::Character;
    std::uint64_t characterId = 0;
    std::uint32_t phase = 0;
    std::uint32_t mapId = 0;
    std::uint32_t zoneId = 0;
    std::uint32_t groupSize = 0;
    std::uint32_t failureCount = 0;
    std::int64_t lastFailureSeconds = 0;
    std::string classification;
    std::string coreRevision;
    std::string playerbotsRevision;
    std::string contentRevision;
    std::string contentGraphDigest;
    std::string profileId;
    std::string capabilityFingerprint;
};

struct ScoringPolicy
{
    std::string version = "cpi-207-v1";
    Score xpWeight = 100;
    Score unlockWeight = 200;
    Score gearWeight = 150;
    Score professionWeight = 100;
    Score continuityWeight = 50;
    Score groupSynergyWeight = 50;
    Score travelSecondCost = 2;
    Score executionSecondCost = 1;
    Score riskMilliDivisor = 10;
    Score contentionMilliDivisor = 10;
    Score priorFailureCost = 500;
    std::int64_t baseFailureBackoffSeconds = 300;
    std::uint32_t maximumBackoffDoublings = 6;
    std::int64_t failureDecaySeconds = 86'400;
};

struct ScoreBreakdown
{
    Score xp = 0;
    Score unlocks = 0;
    Score gear = 0;
    Score profession = 0;
    Score continuity = 0;
    Score groupSynergy = 0;
    Score travel = 0;
    Score execution = 0;
    Score risk = 0;
    Score contention = 0;
    Score priorFailures = 0;
    Score total = 0;
};

struct CandidateEvaluation
{
    std::string stableId;
    bool eligible = false;
    std::vector<std::string> rejectionReasons;
    ScoreBreakdown score;
    std::int64_t retryAtSeconds = 0;
};

enum class PlanDisposition : std::uint8_t
{
    SelectedObjective,
    StockNewRpgFallback,
    DeferExistingControl
};

struct PlanDecision
{
    std::string policyVersion;
    std::string policyIdentity;
    PlanDisposition disposition = PlanDisposition::DeferExistingControl;
    std::optional<std::string> selectedStableId;
    std::vector<CandidateEvaluation> evaluations;
};

[[nodiscard]] std::string CanonicalPolicyIdentity(ScoringPolicy const& policy);

class StrategicGuideDirector
{
public:
    explicit StrategicGuideDirector(ScoringPolicy policy = {});

    [[nodiscard]] CandidateEvaluation Evaluate(
        BotProgressSnapshot const& snapshot,
        ObjectiveCandidate const& candidate,
        FailureEvidence const* failure = nullptr) const;

    [[nodiscard]] PlanDecision Select(
        BotProgressSnapshot const& snapshot,
        std::vector<ObjectiveCandidate> const& candidates,
        std::vector<FailureEvidence> const& failures = {}) const;

    [[nodiscard]] ScoringPolicy const& Policy() const noexcept;

private:
    [[nodiscard]] std::int64_t RetryDelaySeconds(std::uint32_t failureCount) const;

    ScoringPolicy policy_;
};
}
