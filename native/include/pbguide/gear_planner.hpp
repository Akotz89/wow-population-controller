#pragma once

#include "pbguide/director.hpp"

#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <vector>

namespace pbguide
{
enum class ItemSourceKind : std::uint8_t
{
    QuestReward,
    CreatureDrop,
    GameObjectDrop,
    Vendor,
    TrainerRecipe,
    Craft,
    PvpReward,
    DungeonQuest,
    DungeonDrop,
    Disenchant,
    Prospecting,
    Milling,
    Fishing,
    Skinning
};

struct CraftDependency
{
    std::uint32_t itemId = 0;
    std::uint32_t count = 0;
    bool available = true;
    bool phaseAuthorized = true;
    bool earnedSource = true;
};

struct ItemSourceOption
{
    std::string stableId;
    std::uint32_t itemId = 0;
    ItemSourceKind kind = ItemSourceKind::QuestReward;
    std::uint32_t minimumPhase = 1;
    std::uint32_t maximumPhase = 99;
    std::uint32_t requiredGroupSize = 1;
    bool partyReady = true;
    bool available = true;
    bool phaseAuthorized = true;
    bool earnedSource = true;
    std::uint32_t chanceNumerator = 1;
    std::uint32_t chanceDenominator = 1;
    Score travelSeconds = 0;
    Score secondsPerAttempt = 0;
    std::uint64_t moneyCostCopper = 0;
    Score riskMilli = 0;
    Score contentionMilli = 0;
    std::vector<std::string> requiredCapabilities;
    std::uint32_t requiredProfessionSkillId = 0;
    std::uint32_t requiredProfessionSkillValue = 0;
    std::optional<std::string> questAlternativeGroup;
    std::vector<CraftDependency> craftDependencies;
    std::uint32_t minimumCount = 1;
    std::uint32_t maximumCount = 1;
};

class CanonicalItemSourceLoader;

class ItemSourceIndex
{
public:
    [[nodiscard]] static ItemSourceIndex UnverifiedForTesting(
        std::string contentRevision,
        std::string contentGraphDigest,
        std::vector<ItemSourceOption> sources);

    [[nodiscard]] bool IsVerifiedCanonicalProjection() const noexcept;
    [[nodiscard]] std::string const& ContentRevision() const noexcept;
    [[nodiscard]] std::string const& ContentGraphDigest() const noexcept;
    [[nodiscard]] std::vector<ItemSourceOption> const& Sources() const noexcept;
    [[nodiscard]] std::vector<ItemSourceOption const*> SourcesForItem(std::uint32_t itemId) const;

private:
    friend class CanonicalItemSourceLoader;
    friend class CanonicalGearPlanningService;

    ItemSourceIndex(
        std::string contentRevision,
        std::string contentGraphDigest,
        std::vector<ItemSourceOption> sources,
        bool verifiedCanonicalProjection);

    std::string contentRevision_;
    std::string contentGraphDigest_;
    std::vector<ItemSourceOption> sources_;
    std::map<std::uint32_t, std::vector<std::size_t>> sourceIndicesByItem_;
    bool verifiedCanonicalProjection_ = false;
};

struct ItemUpgradeTarget
{
    std::string stableId;
    std::uint32_t itemId = 0;
    std::uint32_t slot = 0;
    Score playerbotsUpgradeScore = 0;
    std::uint32_t expectedReplacementLevel = 0;
    Score professionValue = 0;
};

struct CharacterResourceSnapshot
{
    std::uint64_t moneyCopper = 0;
    std::map<std::uint32_t, std::uint32_t> professionSkillValues;
    std::set<std::string> consumedQuestAlternativeGroups;
    // Missing queried entries are unknown unless a full carried-inventory census was supplied.
    std::map<std::uint32_t, std::uint64_t> itemCounts;
    bool inventoryComplete = false;
};

struct GearScoringPolicy
{
    std::string version = "cpi-211-gear-v1";
    Score upgradeWeight = 100;
    Score usefulLevelWeight = 200;
    Score professionWeight = 100;
    Score timeSecondCost = 1;
    std::uint64_t copperPerCostPoint = 100;
    Score riskMilliDivisor = 10;
    Score contentionMilliDivisor = 10;
    std::uint32_t minimumUsefulLevels = 2;
    Score minimumPursuitScore = 1;
};

struct GearScoreBreakdown
{
    Score upgradeUtility = 0;
    Score usefulLife = 0;
    Score profession = 0;
    Score acquisitionTime = 0;
    Score money = 0;
    Score risk = 0;
    Score contention = 0;
    Score total = 0;
    std::uint64_t expectedAttemptsNumerator = 0;
    std::uint64_t expectedAttemptsDenominator = 1;
    std::uint64_t expectedAttemptsCeiling = 0;
    Score expectedAcquisitionSeconds = 0;
    bool operator==(GearScoreBreakdown const&) const = default;
};

enum class GearDisposition : std::uint8_t
{
    Pursue,
    Deferred,
    Abandoned
};

struct GearSourceEvaluation
{
    std::string targetId;
    std::string stableId;
    std::uint32_t itemId = 0;
    bool eligible = false;
    GearDisposition disposition = GearDisposition::Deferred;
    std::vector<std::string> reasonCodes;
    GearScoreBreakdown score;
    bool operator==(GearSourceEvaluation const&) const = default;
};

enum class GearTargetStatus : std::uint8_t
{
    Candidate,
    Invalid,
    Unavailable
};

struct GearTargetEvaluation
{
    std::string stableId;
    std::uint32_t itemId = 0;
    std::uint32_t slot = 0;
    Score playerbotsUpgradeScore = 0;
    std::uint32_t expectedReplacementLevel = 0;
    Score professionValue = 0;
    GearTargetStatus status = GearTargetStatus::Candidate;
    std::vector<std::string> reasonCodes;
    bool operator==(GearTargetEvaluation const&) const = default;
};

struct GearPlan
{
    std::string planId;
    std::uint64_t characterId = 0;
    std::string contentRevision;
    std::string contentGraphDigest;
    std::int64_t createdAtSeconds = 0;
    std::string policyVersion;
    std::string policyIdentity;
    std::optional<std::string> selectedTargetId;
    std::optional<std::string> selectedSourceId;
    std::optional<std::uint32_t> selectedItemId;
    std::vector<GearTargetEvaluation> targets;
    std::vector<GearSourceEvaluation> evaluations;
    std::vector<std::string> errors;
    bool operator==(GearPlan const&) const = default;
};

[[nodiscard]] std::string CanonicalGearPolicyIdentity(GearScoringPolicy const& policy);

class EarnedGearPlanner
{
public:
    explicit EarnedGearPlanner(
        GearScoringPolicy policy = {},
        bool allowUnverifiedSourceIndexForTesting = false);

    [[nodiscard]] GearPlan Plan(
        BotProgressSnapshot const& snapshot,
        CharacterResourceSnapshot const& resources,
        ItemSourceIndex const& sourceIndex,
        std::vector<ItemUpgradeTarget> const& targets) const;

private:
    friend class CanonicalGearPlanningService;
    [[nodiscard]] GearPlan MakePlanMetadata(BotProgressSnapshot const&) const;
    [[nodiscard]] GearPlan Evaluate(BotProgressSnapshot const&, CharacterResourceSnapshot const&,
        ItemSourceIndex const&, std::vector<ItemUpgradeTarget> const&) const;
    GearScoringPolicy policy_;
    bool allowUnverifiedSourceIndexForTesting_ = false;
};
}
