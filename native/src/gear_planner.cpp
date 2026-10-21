#include "pbguide/gear_planner.hpp"

#include <algorithm>
#include <cctype>
#include <limits>
#include <set>
#include <regex>
#include <stdexcept>
#include <utility>
#include <functional>

namespace pbguide
{
namespace
{
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

bool IsValidSourceKind(ItemSourceKind kind)
{
    switch (kind)
    {
        case ItemSourceKind::QuestReward:
        case ItemSourceKind::CreatureDrop:
        case ItemSourceKind::GameObjectDrop:
        case ItemSourceKind::Vendor:
        case ItemSourceKind::TrainerRecipe:
        case ItemSourceKind::Craft:
        case ItemSourceKind::PvpReward:
        case ItemSourceKind::DungeonQuest:
        case ItemSourceKind::DungeonDrop:
        case ItemSourceKind::Disenchant:
        case ItemSourceKind::Prospecting:
        case ItemSourceKind::Milling:
        case ItemSourceKind::Fishing:
        case ItemSourceKind::Skinning:
            return true;
    }
    return false;
}

bool IsStochasticRepeatableKind(ItemSourceKind kind)
{
    return kind == ItemSourceKind::CreatureDrop || kind == ItemSourceKind::GameObjectDrop ||
        kind == ItemSourceKind::DungeonDrop || kind == ItemSourceKind::Disenchant ||
        kind == ItemSourceKind::Prospecting || kind == ItemSourceKind::Milling ||
        kind == ItemSourceKind::Fishing || kind == ItemSourceKind::Skinning;
}

bool IsQuestKind(ItemSourceKind kind)
{
    return kind == ItemSourceKind::QuestReward || kind == ItemSourceKind::DungeonQuest;
}

bool IsCraftKind(ItemSourceKind kind)
{
    return kind == ItemSourceKind::Craft;
}

bool HasValidSourceKindShape(ItemSourceOption const& source)
{
    if (!source.minimumCount || source.minimumCount > source.maximumCount)
        return false;
    bool const stochastic = source.chanceNumerator < source.chanceDenominator;
    if (stochastic && (!IsStochasticRepeatableKind(source.kind) || source.secondsPerAttempt <= 0))
        return false;
    if (source.questAlternativeGroup.has_value() && !IsQuestKind(source.kind))
        return false;
    if (!source.craftDependencies.empty() && !IsCraftKind(source.kind))
        return false;
    if (IsCraftKind(source.kind) &&
        (source.requiredProfessionSkillId == 0 || source.craftDependencies.empty()))
        return false;
    if (source.requiredProfessionSkillId == 0 && source.requiredProfessionSkillValue != 0)
        return false;
    return true;
}

bool TryMultiply(Score left, Score right, Score& output)
{
    if (left < 0 || right < 0 || (left != 0 && right > std::numeric_limits<Score>::max() / left))
        return false;
    output = left * right;
    return true;
}

bool TryAdd(Score left, Score right, Score& output)
{
    if ((right > 0 && left > std::numeric_limits<Score>::max() - right) ||
        (right < 0 && left < std::numeric_limits<Score>::min() - right))
        return false;
    output = left + right;
    return true;
}

bool TryCeilMultiplyDivide(Score value, std::uint32_t numerator, std::uint32_t denominator, Score& output)
{
    if (value < 0 || numerator == 0 || denominator == 0)
        return false;
    Score product = 0;
    if (!TryMultiply(value, static_cast<Score>(numerator), product))
        return false;
    output = product / static_cast<Score>(denominator);
    if (product % static_cast<Score>(denominator) != 0)
        return TryAdd(output, 1, output);
    return true;
}

void AddReason(GearSourceEvaluation& evaluation, std::string reason)
{
    if (std::find(evaluation.reasonCodes.begin(), evaluation.reasonCodes.end(), reason) ==
        evaluation.reasonCodes.end())
        evaluation.reasonCodes.push_back(std::move(reason));
}

bool IsPermanentReason(std::string const& reason)
{
    return reason == "invalid_source_data" || reason == "invalid_target_data" ||
        reason == "unearned_or_synthetic_source" || reason == "replacement_horizon_too_short" ||
        reason == "craft_dependency_unearned" || reason == "quest_alternative_consumed";
}

bool IsDefaultPolicy(GearScoringPolicy const& policy)
{
    GearScoringPolicy const expected;
    return policy.upgradeWeight == expected.upgradeWeight &&
        policy.usefulLevelWeight == expected.usefulLevelWeight &&
        policy.professionWeight == expected.professionWeight &&
        policy.timeSecondCost == expected.timeSecondCost &&
        policy.copperPerCostPoint == expected.copperPerCostPoint &&
        policy.riskMilliDivisor == expected.riskMilliDivisor &&
        policy.contentionMilliDivisor == expected.contentionMilliDivisor &&
        policy.minimumUsefulLevels == expected.minimumUsefulLevels &&
        policy.minimumPursuitScore == expected.minimumPursuitScore;
}

struct ResourceState
{
    std::map<std::uint32_t, std::uint64_t> stock;
    std::uint64_t money = 0;
    Score seconds = 0;
    std::map<std::string, std::uint64_t> procurement;
    Score risk = 0;
    Score contention = 0;
};

// Backtrack across siblings as well as source alternatives; each branch has its own ledger.
class ResourceSearch
{
    using Next = std::function<bool(ResourceState)>;
    BotProgressSnapshot const& snapshot;
    CharacterResourceSnapshot const& resources;
    ItemSourceIndex const& index;
    GearScoringPolicy const& policy;
    std::size_t& work;
    std::string unknown;
    std::optional<ResourceState> best;
    Score bestCost = 0;
    bool truncated = false;

    bool Unknown(char const* reason)
    {
        std::string value(reason);
        if (value.find("_bound_") != std::string::npos) truncated = true;
        if (unknown.empty() || value == "craft_resources_work_bound_unknown" ||
            (value == "craft_resources_quantity_overflow_unknown" && unknown != "craft_resources_work_bound_unknown"))
            unknown = std::move(value);
        return false;
    }
    bool Step() { return work ? (--work, true) : Unknown("craft_resources_work_bound_unknown"); }
    std::optional<Score> Cost(ResourceState const& state)
    {
        auto spent = resources.moneyCopper - state.money;
        auto money = spent / policy.copperPerCostPoint + (spent % policy.copperPerCostPoint != 0);
        Score time = 0, total = 0;
        if (money > static_cast<std::uint64_t>(std::numeric_limits<Score>::max()) ||
            !TryMultiply(state.seconds, policy.timeSecondCost, time) ||
            !TryAdd(time, static_cast<Score>(money), total) ||
            !TryAdd(total, state.risk / policy.riskMilliDivisor, total) ||
            !TryAdd(total, state.contention / policy.contentionMilliDivisor, total))
        {
            Unknown("craft_resources_cost_overflow_unknown"); return std::nullopt;
        }
        return total;
    }
    bool Produce(ItemSourceOption const& source, std::uint64_t count, ResourceState state,
        std::set<std::uint32_t> const& path, Next const& next, bool dependency, unsigned depth)
    {
        if (best && bestCost == 0) return false;
        if (!Step()) return false;
        if (!source.minimumPhase || source.minimumPhase > source.maximumPhase || !source.requiredGroupSize ||
            source.travelSeconds < 0 || source.secondsPerAttempt < 0 || source.riskMilli < 0 || source.contentionMilli < 0)
            return Unknown("craft_resources_invalid_source_unknown");
        if (!source.available || !source.phaseAuthorized || !source.earnedSource ||
            snapshot.phase < source.minimumPhase || snapshot.phase > source.maximumPhase ||
            snapshot.groupSize < source.requiredGroupSize || !source.partyReady)
            return false;
        if (source.chanceNumerator == 0 || source.chanceDenominator == 0 ||
            source.chanceNumerator != source.chanceDenominator ||
            (dependency && source.kind != ItemSourceKind::Craft && source.kind != ItemSourceKind::Vendor))
            return Unknown("craft_resources_procurement_context_unknown");
        if (source.questAlternativeGroup && resources.consumedQuestAlternativeGroups.contains(*source.questAlternativeGroup))
            return false;
        for (auto const& capability : source.requiredCapabilities)
        {
            auto found = snapshot.capabilities.find(capability);
            if (found == snapshot.capabilities.end()) return Unknown("craft_resources_capability_unknown");
            if (!found->second) return false;
        }
        if (source.requiredProfessionSkillId)
        {
            auto found = resources.professionSkillValues.find(source.requiredProfessionSkillId);
            if (found == resources.professionSkillValues.end()) return Unknown("craft_resources_profession_unknown");
            if (found->second < source.requiredProfessionSkillValue) return false;
        }
        if (count > static_cast<std::uint64_t>(std::numeric_limits<Score>::max()) ||
            (source.moneyCostCopper && count > std::numeric_limits<std::uint64_t>::max() / source.moneyCostCopper))
            return Unknown("craft_resources_quantity_overflow_unknown");
        auto cost = count * source.moneyCostCopper;
        if (state.money < cost) return false;
        state.money -= cost;
        Score duration = 0;
        if (!TryMultiply(source.secondsPerAttempt, static_cast<Score>(count), duration) ||
            !TryAdd(duration, source.travelSeconds, duration) || !TryAdd(state.seconds, duration, state.seconds))
            return Unknown("craft_resources_cost_overflow_unknown");
        Score risk = 0, contention = 0;
        if (!TryMultiply(source.riskMilli, static_cast<Score>(count), risk) ||
            !TryMultiply(source.contentionMilli, static_cast<Score>(count), contention) ||
            !TryAdd(state.risk, risk, state.risk) || !TryAdd(state.contention, contention, state.contention))
            return Unknown("craft_resources_cost_overflow_unknown");
        auto lowerBound = Cost(state);
        if (!lowerBound || (best && *lowerBound >= bestCost)) return false;
        if (!state.procurement.contains(source.stableId) && state.procurement.size() >= 256)
            return Unknown("craft_resources_step_bound_unknown");
        auto& planned = state.procurement[source.stableId];
        if (planned > std::numeric_limits<std::uint64_t>::max() - count)
            return Unknown("craft_resources_quantity_overflow_unknown");
        planned += count;
        std::vector<CraftDependency> deps = source.craftDependencies;
        std::sort(deps.begin(), deps.end(), [](auto const& a, auto const& b) { return a.itemId < b.itemId; });
        std::function<bool(std::size_t, ResourceState)> satisfy;
        satisfy = [&](std::size_t at, ResourceState current) {
            if (!Step()) return false;
            if (at == deps.size()) return next(std::move(current));
            auto const& dep = deps[at];
            if (!dep.itemId || !dep.count || !dep.available || !dep.phaseAuthorized || !dep.earnedSource)
                return false;
            if (count > std::numeric_limits<std::uint64_t>::max() / dep.count)
                return Unknown("craft_resources_quantity_overflow_unknown");
            return Need(dep.itemId, count * dep.count, std::move(current), path,
                [&](ResourceState remaining) { return satisfy(at + 1, std::move(remaining)); }, depth + 1);
        };
        return satisfy(0, std::move(state));
    }
    bool Need(std::uint32_t item, std::uint64_t count, ResourceState state,
        std::set<std::uint32_t> path, Next const& next, unsigned depth)
    {
        if (!Step()) return false;
        auto found = state.stock.find(item);
        if (found == state.stock.end() && !resources.inventoryComplete)
            return Unknown("craft_resources_inventory_unknown");
        auto held = found == state.stock.end() ? 0 : found->second;
        auto used = std::min(held, count);
        state.stock[item] = held - used;
        count -= used;
        if (!count) return next(std::move(state));
        if (depth >= 32) return Unknown("craft_resources_depth_bound_unknown");
        if (!path.insert(item).second) return Unknown("craft_resources_cycle_unknown");
        auto alternatives = index.SourcesForItem(item);
        if (alternatives.empty()) return Unknown("craft_resources_sources_unknown");
        for (auto const* source : alternatives)
        {
            if (best && bestCost == 0) return false;
            if (!Step()) return false;
            if (!source->minimumCount || source->minimumCount > source->maximumCount)
                return Unknown("craft_resources_invalid_source_unknown");
            auto batches = count / source->minimumCount + (count % source->minimumCount != 0);
            if (batches > std::numeric_limits<std::uint64_t>::max() / source->minimumCount)
                return Unknown("craft_resources_quantity_overflow_unknown");
            auto surplus = batches * source->minimumCount - count;
            if (Produce(*source, batches, state, path, [&](ResourceState remaining) {
                if (remaining.stock[item] > std::numeric_limits<std::uint64_t>::max() - surplus)
                    return Unknown("craft_resources_quantity_overflow_unknown");
                remaining.stock[item] += surplus;
                return next(std::move(remaining));
            }, true, depth)) return true;
            if (alternatives.size() > 1 && batches > 1)
            {
                auto parentPath = path;
                parentPath.erase(item);
                if (Produce(*source, 1, state, path, [&](ResourceState remaining) {
                    return Need(item, count - source->minimumCount, std::move(remaining), parentPath, next, depth + 1);
                }, true, depth)) return true;
            }
        }
        return false;
    }
public:
    ResourceSearch(BotProgressSnapshot const& s, CharacterResourceSnapshot const& r,
        ItemSourceIndex const& i, GearScoringPolicy const& p, std::size_t& budget)
        : snapshot(s), resources(r), index(i), policy(p), work(budget) {}
    std::optional<ResourceState> Solve(ItemSourceOption const& source)
    {
        ResourceState initial{resources.itemCounts, resources.moneyCopper, 0, {}};
        Produce(source, 1, std::move(initial), {source.itemId}, [&](ResourceState state) {
            auto cost = Cost(state);
            if (cost && (!best || *cost < bestCost)) { best = std::move(state); bestCost = *cost; }
            return false;
        }, false, 0);
        if (truncated && (!best || bestCost != 0)) return std::nullopt;
        return best;
    }
    std::string Failure() const { return unknown.empty() ? "craft_resources_infeasible" : unknown; }
};
}

ItemSourceIndex ItemSourceIndex::UnverifiedForTesting(
    std::string contentRevision,
    std::string contentGraphDigest,
    std::vector<ItemSourceOption> sources)
{
    return ItemSourceIndex(
        std::move(contentRevision), std::move(contentGraphDigest), std::move(sources), false);
}

ItemSourceIndex::ItemSourceIndex(
    std::string contentRevision,
    std::string contentGraphDigest,
    std::vector<ItemSourceOption> sources,
    bool verifiedCanonicalProjection)
    : contentRevision_(std::move(contentRevision)),
      contentGraphDigest_(std::move(contentGraphDigest)),
      sources_(std::move(sources)),
      verifiedCanonicalProjection_(verifiedCanonicalProjection)
{
    if (contentRevision_.empty() || !IsLowerHexDigest(contentGraphDigest_))
        throw std::invalid_argument("item-source index identity is invalid");
    std::set<std::string> sourceIds;
    for (ItemSourceOption const& source : sources_)
    {
        if (!IsValidStableId(source.stableId) || !sourceIds.insert(source.stableId).second || source.itemId == 0 ||
            !IsValidSourceKind(source.kind) || !HasValidSourceKindShape(source))
            throw std::invalid_argument("item-source index contains an invalid or duplicate source");
    }
    std::sort(sources_.begin(), sources_.end(), [](ItemSourceOption const& left, ItemSourceOption const& right) {
        if (left.itemId != right.itemId)
            return left.itemId < right.itemId;
        return left.stableId < right.stableId;
    });
    for (std::size_t index = 0; index < sources_.size(); ++index)
        sourceIndicesByItem_[sources_[index].itemId].push_back(index);
}

bool ItemSourceIndex::IsVerifiedCanonicalProjection() const noexcept
{
    return verifiedCanonicalProjection_;
}

std::string const& ItemSourceIndex::ContentRevision() const noexcept
{
    return contentRevision_;
}

std::string const& ItemSourceIndex::ContentGraphDigest() const noexcept
{
    return contentGraphDigest_;
}

std::vector<ItemSourceOption> const& ItemSourceIndex::Sources() const noexcept
{
    return sources_;
}

std::vector<ItemSourceOption const*> ItemSourceIndex::SourcesForItem(std::uint32_t itemId) const
{
    std::vector<ItemSourceOption const*> result;
    auto const found = sourceIndicesByItem_.find(itemId);
    if (found == sourceIndicesByItem_.end())
        return result;
    result.reserve(found->second.size());
    for (std::size_t const index : found->second)
        result.push_back(&sources_[index]);
    return result;
}

std::string CanonicalGearPolicyIdentity(GearScoringPolicy const& policy)
{
    return policy.version + "|upgrade=" + std::to_string(policy.upgradeWeight) +
        "|useful_level=" + std::to_string(policy.usefulLevelWeight) +
        "|profession=" + std::to_string(policy.professionWeight) +
        "|time=" + std::to_string(policy.timeSecondCost) +
        "|copper=" + std::to_string(policy.copperPerCostPoint) +
        "|risk_div=" + std::to_string(policy.riskMilliDivisor) +
        "|contention_div=" + std::to_string(policy.contentionMilliDivisor) +
        "|minimum_useful_levels=" + std::to_string(policy.minimumUsefulLevels) +
        "|minimum_pursuit_score=" + std::to_string(policy.minimumPursuitScore);
}

EarnedGearPlanner::EarnedGearPlanner(
    GearScoringPolicy policy,
    bool allowUnverifiedSourceIndexForTesting)
    : policy_(std::move(policy)),
      allowUnverifiedSourceIndexForTesting_(allowUnverifiedSourceIndexForTesting)
{
    if (policy_.version.empty() || policy_.upgradeWeight < 0 || policy_.usefulLevelWeight < 0 ||
        policy_.professionWeight < 0 || policy_.timeSecondCost < 0 || policy_.copperPerCostPoint == 0 ||
        policy_.riskMilliDivisor <= 0 || policy_.contentionMilliDivisor <= 0 ||
        policy_.minimumPursuitScore < 0)
        throw std::invalid_argument("gear scoring policy is invalid");
    if (policy_.version == "cpi-211-gear-v1" && !IsDefaultPolicy(policy_))
        throw std::invalid_argument("reserved gear policy version has noncanonical constants");
}

GearPlan EarnedGearPlanner::Plan(
    BotProgressSnapshot const& snapshot,
    CharacterResourceSnapshot const& resources,
    ItemSourceIndex const& sourceIndex,
    std::vector<ItemUpgradeTarget> const& targets) const
{
    if (!sourceIndex.IsVerifiedCanonicalProjection() && !allowUnverifiedSourceIndexForTesting_)
    {
        GearPlan rejected = MakePlanMetadata(snapshot);
        rejected.errors.push_back("unverified_source_index");
        return rejected;
    }
    return Evaluate(snapshot, resources, sourceIndex, targets);
}

GearPlan EarnedGearPlanner::MakePlanMetadata(BotProgressSnapshot const& snapshot) const
{
    GearPlan plan;
    plan.planId = "gear_plan:" + std::to_string(snapshot.characterId) + ":" +
        std::to_string(snapshot.nowSeconds);
    plan.characterId = snapshot.characterId;
    plan.contentRevision = snapshot.contentRevision;
    plan.contentGraphDigest = snapshot.contentGraphDigest;
    plan.createdAtSeconds = snapshot.nowSeconds;
    plan.policyVersion = policy_.version;
    plan.policyIdentity = CanonicalGearPolicyIdentity(policy_);
    return plan;
}

GearPlan EarnedGearPlanner::Evaluate(BotProgressSnapshot const& snapshot,
    CharacterResourceSnapshot const& resources, ItemSourceIndex const& sourceIndex,
    std::vector<ItemUpgradeTarget> const& targets) const
{
    GearPlan plan = MakePlanMetadata(snapshot);
    std::size_t resourceWork = 16384; // Shared across the entire call, including failed alternatives.
    if (!IsValidBotProgressSnapshot(snapshot))
    {
        plan.errors.push_back("invalid_snapshot");
        return plan;
    }
    if (!IsValidBotProgressSnapshotIdentity(snapshot))
    {
        plan.errors.push_back("invalid_snapshot_identity");
        return plan;
    }
    if (!snapshot.populationAdmitted)
    {
        plan.errors.push_back("population_not_admitted");
        return plan;
    }
    if (!snapshot.scheduleActive)
    {
        plan.errors.push_back("schedule_inactive");
        return plan;
    }
    if (IsGuideControlSuspended(snapshot))
    {
        plan.errors.push_back("guide_control_suspended");
        return plan;
    }
    if (snapshot.contentRevision != sourceIndex.ContentRevision() ||
        snapshot.contentGraphDigest != sourceIndex.ContentGraphDigest())
    {
        plan.errors.push_back("content_graph_identity_mismatch");
        return plan;
    }

    std::map<std::string, ItemUpgradeTarget const*> targetById;
    for (std::size_t targetIndex = 0; targetIndex < targets.size(); ++targetIndex)
    {
        ItemUpgradeTarget const& target = targets[targetIndex];
        GearTargetEvaluation targetEvaluation;
        targetEvaluation.stableId = target.stableId;
        targetEvaluation.itemId = target.itemId;
        targetEvaluation.slot = target.slot;
        targetEvaluation.playerbotsUpgradeScore = target.playerbotsUpgradeScore;
        targetEvaluation.expectedReplacementLevel = target.expectedReplacementLevel;
        targetEvaluation.professionValue = target.professionValue;
        if (target.stableId.size() > 160 ||
            !std::regex_match(target.stableId, std::regex("[a-z][a-z0-9_-]*:[a-zA-Z0-9_.:-]+")) ||
            target.itemId == 0 || target.slot > 18 ||
            target.playerbotsUpgradeScore <= 0 ||
            target.professionValue < 0 || target.expectedReplacementLevel <= snapshot.level ||
            target.expectedReplacementLevel > 80)
        {
            targetEvaluation.status = GearTargetStatus::Invalid;
            targetEvaluation.reasonCodes.push_back("invalid_upgrade_target");
            plan.targets.push_back(std::move(targetEvaluation));
            continue;
        }
        if (!targetById.emplace(target.stableId, &target).second)
        {
            plan.errors.push_back("duplicate_upgrade_target:" + target.stableId);
            return plan;
        }
        if (sourceIndex.SourcesForItem(target.itemId).empty())
        {
            targetEvaluation.status = GearTargetStatus::Unavailable;
            targetEvaluation.reasonCodes.push_back("no_sources_for_target");
        }
        plan.targets.push_back(std::move(targetEvaluation));
    }
    std::sort(plan.targets.begin(), plan.targets.end(), [](GearTargetEvaluation const& left,
                                                               GearTargetEvaluation const& right) {
        if (left.stableId != right.stableId)
            return left.stableId < right.stableId;
        if (left.slot != right.slot)
            return left.slot < right.slot;
        return left.itemId < right.itemId;
    });

    for (auto const& [targetId, targetPointer] : targetById)
    {
        ItemUpgradeTarget const& target = *targetPointer;
        std::vector<ItemSourceOption const*> const targetSources = sourceIndex.SourcesForItem(target.itemId);
        for (ItemSourceOption const* sourcePointer : targetSources)
        {
            ItemSourceOption const& source = *sourcePointer;
            GearSourceEvaluation evaluation;
            evaluation.targetId = targetId;
            evaluation.stableId = source.stableId;
            evaluation.itemId = source.itemId;
            std::optional<ResourceState> resourcePlan;

        if (target.itemId == 0 || target.slot > 18 || target.playerbotsUpgradeScore <= 0 ||
            target.professionValue < 0 || target.expectedReplacementLevel <= snapshot.level)
            AddReason(evaluation, "invalid_target_data");
        if (source.minimumPhase == 0 || source.maximumPhase == 0 || source.minimumPhase > source.maximumPhase ||
            source.requiredGroupSize == 0 || source.chanceNumerator == 0 || source.chanceDenominator == 0 ||
            source.chanceNumerator > source.chanceDenominator || source.travelSeconds < 0 ||
            source.secondsPerAttempt < 0 || source.riskMilli < 0 || source.contentionMilli < 0)
            AddReason(evaluation, "invalid_source_data");
        if (source.questAlternativeGroup.has_value() && !IsValidStableId(*source.questAlternativeGroup))
            AddReason(evaluation, "invalid_source_data");
        if (!source.earnedSource)
            AddReason(evaluation, "unearned_or_synthetic_source");
        if (!source.available)
            AddReason(evaluation, "source_unavailable");
        if (!source.phaseAuthorized || snapshot.phase < source.minimumPhase || snapshot.phase > source.maximumPhase)
            AddReason(evaluation, "phase_not_authorized");
        if (snapshot.groupSize < source.requiredGroupSize || !source.partyReady)
            AddReason(evaluation, "party_not_ready");
        if (resources.moneyCopper < source.moneyCostCopper)
            AddReason(evaluation, "insufficient_money");
        if (target.expectedReplacementLevel - snapshot.level < policy_.minimumUsefulLevels)
            AddReason(evaluation, "replacement_horizon_too_short");
        if (source.questAlternativeGroup.has_value() &&
            resources.consumedQuestAlternativeGroups.contains(*source.questAlternativeGroup))
            AddReason(evaluation, "quest_alternative_consumed");

        std::set<std::string> capabilities;
        for (std::string const& capability : source.requiredCapabilities)
        {
            if (capability.empty() || !capabilities.insert(capability).second)
                AddReason(evaluation, "invalid_source_data");
        }
        for (std::string const& capability : capabilities)
        {
            auto const present = snapshot.capabilities.find(capability);
            if (present == snapshot.capabilities.end() || !present->second)
                AddReason(evaluation, "missing_capability:" + capability);
        }

        if (source.requiredProfessionSkillId != 0)
        {
            auto const skill = resources.professionSkillValues.find(source.requiredProfessionSkillId);
            if (skill == resources.professionSkillValues.end() || skill->second < source.requiredProfessionSkillValue)
                AddReason(evaluation, "profession_skill_too_low");
        }
        for (CraftDependency const& dependency : source.craftDependencies)
        {
            if (dependency.itemId == 0 || dependency.count == 0)
                AddReason(evaluation, "invalid_source_data");
            if (!dependency.earnedSource)
                AddReason(evaluation, "craft_dependency_unearned");
            if (!dependency.available)
                AddReason(evaluation, "craft_dependency_unavailable");
            if (!dependency.phaseAuthorized)
                AddReason(evaluation, "craft_dependency_phase_blocked");
        }

        if (evaluation.reasonCodes.empty() && IsCraftKind(source.kind))
        {
            ResourceSearch search(snapshot, resources, sourceIndex, policy_, resourceWork);
            resourcePlan = search.Solve(source);
            if (!resourcePlan) AddReason(evaluation, search.Failure());
        }

        evaluation.eligible = evaluation.reasonCodes.empty();
        if (!evaluation.eligible)
        {
            evaluation.disposition = std::any_of(evaluation.reasonCodes.begin(), evaluation.reasonCodes.end(),
                IsPermanentReason) ? GearDisposition::Abandoned : GearDisposition::Deferred;
            plan.evaluations.push_back(std::move(evaluation));
            continue;
        }

        std::uint64_t const expectedAttemptsCeiling =
            (static_cast<std::uint64_t>(source.chanceDenominator) + source.chanceNumerator - 1) /
            source.chanceNumerator;
        evaluation.score.expectedAttemptsNumerator = source.chanceDenominator;
        evaluation.score.expectedAttemptsDenominator = source.chanceNumerator;
        evaluation.score.expectedAttemptsCeiling = expectedAttemptsCeiling;
        Score expectedAttemptSeconds = 0;
        if (!TryCeilMultiplyDivide(source.secondsPerAttempt, source.chanceDenominator,
                source.chanceNumerator, expectedAttemptSeconds))
        {
            evaluation.eligible = false;
            evaluation.disposition = GearDisposition::Abandoned;
            AddReason(evaluation, "score_overflow");
            plan.evaluations.push_back(std::move(evaluation));
            continue;
        }
        if (!TryAdd(source.travelSeconds, expectedAttemptSeconds, evaluation.score.expectedAcquisitionSeconds))
        {
            evaluation.eligible = false;
            evaluation.disposition = GearDisposition::Abandoned;
            AddReason(evaluation, "score_overflow");
            plan.evaluations.push_back(std::move(evaluation));
            continue;
        }
        if (resourcePlan) evaluation.score.expectedAcquisitionSeconds = resourcePlan->seconds;

        Score weightedUpgrade = 0;
        Score weightedUsefulLife = 0;
        Score weightedProfession = 0;
        Score weightedTime = 0;
        bool validScore = TryMultiply(target.playerbotsUpgradeScore, policy_.upgradeWeight, weightedUpgrade) &&
            TryMultiply(static_cast<Score>(target.expectedReplacementLevel - snapshot.level),
                policy_.usefulLevelWeight, weightedUsefulLife) &&
            TryMultiply(target.professionValue, policy_.professionWeight, weightedProfession) &&
            TryMultiply(evaluation.score.expectedAcquisitionSeconds, policy_.timeSecondCost, weightedTime);
        if (validScore)
            evaluation.score.upgradeUtility = weightedUpgrade;
        evaluation.score.usefulLife = weightedUsefulLife;
        evaluation.score.profession = weightedProfession;
        evaluation.score.acquisitionTime = -weightedTime;
        auto const totalCost = resourcePlan ? resources.moneyCopper - resourcePlan->money : source.moneyCostCopper;
        std::uint64_t const moneyPoints = totalCost / policy_.copperPerCostPoint +
            (totalCost % policy_.copperPerCostPoint == 0 ? 0 : 1);
        if (moneyPoints > static_cast<std::uint64_t>(std::numeric_limits<Score>::max()))
            validScore = false;
        else
            evaluation.score.money = -static_cast<Score>(moneyPoints);
        evaluation.score.risk = -((resourcePlan ? resourcePlan->risk : source.riskMilli) / policy_.riskMilliDivisor);
        evaluation.score.contention = -((resourcePlan ? resourcePlan->contention : source.contentionMilli) / policy_.contentionMilliDivisor);

        Score total = 0;
        for (Score const component : {evaluation.score.upgradeUtility, evaluation.score.usefulLife,
                 evaluation.score.profession, evaluation.score.acquisitionTime, evaluation.score.money,
                 evaluation.score.risk, evaluation.score.contention})
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
            evaluation.disposition = GearDisposition::Abandoned;
            AddReason(evaluation, "score_overflow");
            // Failed calculations have no partial utility/cost payload. Preserve
            // the independently computed attempts and acquisition diagnostics.
            evaluation.score.upgradeUtility = evaluation.score.usefulLife = evaluation.score.profession = 0;
            evaluation.score.acquisitionTime = evaluation.score.money = evaluation.score.risk = 0;
            evaluation.score.contention = evaluation.score.total = 0;
        }
        else
        {
            evaluation.score.total = total;
            evaluation.disposition = GearDisposition::Deferred;
            if (resourcePlan)
            {
                AddReason(evaluation, "craft_resources_feasible");
                for (auto const& [id, count] : resourcePlan->procurement)
                    AddReason(evaluation, "resource_procurement:" + id + ":" + std::to_string(count));
            }
        }
        plan.evaluations.push_back(std::move(evaluation));
        }
    }

    std::sort(plan.evaluations.begin(), plan.evaluations.end(), [](GearSourceEvaluation const& left,
                                                                      GearSourceEvaluation const& right) {
        if (left.eligible != right.eligible)
            return left.eligible > right.eligible;
        if (left.eligible && left.score.total != right.score.total)
            return left.score.total > right.score.total;
        if (left.targetId != right.targetId)
            return left.targetId < right.targetId;
        return left.stableId < right.stableId;
    });
    if (!plan.evaluations.empty() && plan.evaluations.front().eligible &&
        plan.evaluations.front().score.total >= policy_.minimumPursuitScore)
    {
        GearSourceEvaluation& selected = plan.evaluations.front();
        selected.disposition = GearDisposition::Pursue;
        AddReason(selected, "selected_highest_value");
        plan.selectedTargetId = selected.targetId;
        plan.selectedSourceId = selected.stableId;
        plan.selectedItemId = selected.itemId;
        for (std::size_t index = 1; index < plan.evaluations.size(); ++index)
        {
            GearSourceEvaluation& alternative = plan.evaluations[index];
            if (alternative.eligible)
                AddReason(alternative, "lower_ranked_than:" + selected.stableId);
        }
    }
    else
    {
        for (GearSourceEvaluation& evaluation : plan.evaluations)
        {
            if (evaluation.eligible)
                AddReason(evaluation, "below_pursuit_threshold");
        }
    }
    return plan;
}
}
