#include "pbguide/route_graph.hpp"

#include <algorithm>
#include <cctype>
#include <deque>
#include <set>
#include <stdexcept>
#include <utility>

namespace pbguide
{
namespace
{
constexpr std::size_t MaximumRouteSteps = 10'000;
constexpr std::size_t MaximumPrerequisitesPerStep = 64;
constexpr std::size_t MaximumAlternativeGroupsPerStep = 64;

bool IsLowerHexDigest(std::string const& value)
{
    return value.size() == 64 && std::all_of(value.begin(), value.end(), [](char character) {
        unsigned char const byte = static_cast<unsigned char>(character);
        return std::isdigit(byte) || (character >= 'a' && character <= 'f');
    });
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

bool IsValidContentKind(ContentKind kind)
{
    switch (kind)
    {
        case ContentKind::Quest:
        case ContentKind::Item:
        case ContentKind::Creature:
        case ContentKind::GameObject:
        case ContentKind::Spell:
        case ContentKind::Trainer:
        case ContentKind::Vendor:
        case ContentKind::TaxiNode:
        case ContentKind::Area:
        case ContentKind::Map:
        case ContentKind::Dungeon:
        case ContentKind::LootTemplate:
        case ContentKind::Recipe:
            return true;
    }
    return false;
}

bool IsSatisfied(GuideProgress const& progress, GuideStep const& prerequisite)
{
    return progress.completed.contains(prerequisite.stableId) ||
        (progress.skipped.contains(prerequisite.stableId) && prerequisite.skippedSatisfiesDependents);
}

std::string JoinStableIds(std::vector<std::string> stableIds)
{
    std::sort(stableIds.begin(), stableIds.end());
    std::string result;
    for (std::size_t index = 0; index < stableIds.size(); ++index)
    {
        if (index != 0)
            result += '|';
        result += stableIds[index];
    }
    return result;
}
}

GuideStep::GuideStep(
    std::string stableIdValue,
    ObjectiveCandidate objectiveValue,
    std::vector<std::string> prerequisitesValue,
    bool maySkipValue,
    bool skippedSatisfiesDependentsValue,
    std::vector<ContentRef> contentRefsValue)
    : stableId(std::move(stableIdValue)),
      objective(std::move(objectiveValue)),
      prerequisites(std::move(prerequisitesValue)),
      maySkip(maySkipValue),
      skippedSatisfiesDependents(skippedSatisfiesDependentsValue),
      contentRefs(std::move(contentRefsValue))
{
}

GuideRoute::GuideRoute(RouteIdentity identity, std::vector<GuideStep> steps)
    : identity_(std::move(identity)), steps_(std::move(steps))
{
    if (!IsValidStableId(identity_.routeId) || identity_.revision.empty() ||
        !IsLowerHexDigest(identity_.catalogueDigest))
        throw std::invalid_argument("guide route identity is incomplete or has an invalid digest");
    if (steps_.empty())
        throw std::invalid_argument("guide route must contain at least one step");
    if (steps_.size() > MaximumRouteSteps)
        throw std::invalid_argument("guide route exceeds the maximum step count");
    for (std::size_t index = 0; index < steps_.size(); ++index)
    {
        GuideStep& step = steps_[index];
        if (step.objective.stableId.empty())
            step.objective.stableId = step.stableId;
        if (!IsValidStableId(step.stableId) || step.objective.stableId != step.stableId)
            throw std::invalid_argument("guide step and objective must share one non-empty stable ID");
        if (!indexById_.emplace(step.stableId, index).second)
            throw std::invalid_argument("duplicate guide step: " + step.stableId);
    }
    Validate();
}

RouteResolution GuideRoute::Resolve(GuideProgress const& progress) const
{
    RouteResolution resolution;

    if (progress.identity.routeId != identity_.routeId || progress.identity.revision != identity_.revision ||
        progress.identity.catalogueDigest != identity_.catalogueDigest)
    {
        resolution.progressErrors.push_back("route_identity_mismatch");
        return resolution;
    }

    for (std::string const& stableId : progress.completed)
    {
        if (!indexById_.contains(stableId))
            resolution.progressErrors.push_back("unknown_completed_step:" + stableId);
        if (progress.skipped.contains(stableId))
            resolution.progressErrors.push_back("step_both_completed_and_skipped:" + stableId);
    }
    for (std::string const& stableId : progress.skipped)
    {
        if (!indexById_.contains(stableId))
            resolution.progressErrors.push_back("unknown_skipped_step:" + stableId);
        else if (!steps_[indexById_.at(stableId)].maySkip)
            resolution.progressErrors.push_back("non_skippable_step_skipped:" + stableId);
    }
    if (!resolution.progressErrors.empty())
        return resolution;

    std::map<std::string, std::string> selectedExclusiveBranches;
    for (GuideStep const& step : steps_)
    {
        if (!step.exclusiveGroup.has_value() || !progress.completed.contains(step.stableId))
            continue;
        auto const [found, inserted] = selectedExclusiveBranches.emplace(*step.exclusiveGroup, step.stableId);
        if (!inserted && found->second != step.stableId)
            resolution.progressErrors.push_back(
                "multiple_exclusive_branches_completed:" + *step.exclusiveGroup);
    }
    if (!resolution.progressErrors.empty())
        return resolution;

    for (GuideStep const& step : steps_)
    {
        if (progress.completed.contains(step.stableId) || progress.skipped.contains(step.stableId))
            continue;

        std::vector<std::string> reasons;
        if (step.exclusiveGroup.has_value())
        {
            auto const selected = selectedExclusiveBranches.find(*step.exclusiveGroup);
            if (selected != selectedExclusiveBranches.end() && selected->second != step.stableId)
                reasons.push_back("exclusive_branch_selected:" + selected->second);
        }
        for (std::string const& prerequisiteId : step.prerequisites)
        {
            GuideStep const& prerequisite = steps_[indexById_.at(prerequisiteId)];
            if (IsSatisfied(progress, prerequisite))
                continue;
            reasons.push_back("prerequisite_incomplete:" + prerequisiteId);
        }
        for (std::vector<std::string> const& alternatives : step.prerequisiteAnyOf)
        {
            bool const satisfied = std::any_of(alternatives.begin(), alternatives.end(), [&](std::string const& id) {
                return IsSatisfied(progress, steps_[indexById_.at(id)]);
            });
            if (!satisfied)
                reasons.push_back("prerequisite_any_incomplete:" + JoinStableIds(alternatives));
        }

        if (reasons.empty())
        {
            resolution.ready.push_back(step.objective);
            if (step.clusterId.has_value())
                resolution.readyClusterByObjective.emplace(step.stableId, *step.clusterId);
        }
        else
            resolution.blockedReasons.emplace(step.stableId, std::move(reasons));
    }

    std::sort(resolution.ready.begin(), resolution.ready.end(), [](ObjectiveCandidate const& left,
                                                                      ObjectiveCandidate const& right) {
        return left.stableId < right.stableId;
    });
    return resolution;
}

RouteSynchronization GuideRoute::Synchronize(
    GuideProgress const* persisted,
    CharacterQuestHistory const& history) const
{
    RouteSynchronization synchronization;
    synchronization.progress.identity = identity_;
    bool const exactIdentity = persisted != nullptr &&
        persisted->identity.routeId == identity_.routeId && persisted->identity.revision == identity_.revision &&
        persisted->identity.catalogueDigest == identity_.catalogueDigest;
    if (exactIdentity)
    {
        synchronization.progress.completed = persisted->completed;
        synchronization.progress.skipped = persisted->skipped;
        RouteResolution const persistedValidation = Resolve(synchronization.progress);
        if (!persistedValidation.progressErrors.empty())
        {
            synchronization.errors = persistedValidation.progressErrors;
            return synchronization;
        }
    }
    else if (persisted != nullptr)
    {
        synchronization.rebuiltFromAuthoritativeHistory = true;
    }

    std::set<std::string> authoritativeTargets;
    for (GuideStep const& step : steps_)
    {
        std::vector<std::uint32_t> questIds;
        for (ContentRef const& contentRef : step.contentRefs)
        {
            if (contentRef.kind == ContentKind::Quest)
                questIds.push_back(contentRef.id);
        }
        if (questIds.empty())
            continue;

        bool const rewarded = std::any_of(questIds.begin(), questIds.end(), [&](std::uint32_t questId) {
            return history.rewardedQuestIds.contains(questId);
        });
        bool const active = std::any_of(questIds.begin(), questIds.end(), [&](std::uint32_t questId) {
            return history.activeQuestIds.contains(questId);
        });
        if (rewarded)
        {
            synchronization.progress.completed.insert(step.stableId);
            synchronization.progress.skipped.erase(step.stableId);
            authoritativeTargets.insert(step.stableId);
        }
        else
        {
            synchronization.progress.completed.erase(step.stableId);
            if (active)
                authoritativeTargets.insert(step.stableId);
        }
    }

    std::set<std::string> repairRequired;
    std::set<std::vector<std::string>> alternativeGroups;
    std::vector<std::string> pending(authoritativeTargets.begin(), authoritativeTargets.end());
    std::set<std::string> visited;
    while (!pending.empty())
    {
        std::string const stableId = pending.back();
        pending.pop_back();
        if (!visited.insert(stableId).second)
            continue;
        GuideStep const& step = steps_[indexById_.at(stableId)];
        for (std::string const& prerequisiteId : step.prerequisites)
        {
            GuideStep const& prerequisite = steps_[indexById_.at(prerequisiteId)];
            if (!IsSatisfied(synchronization.progress, prerequisite))
            {
                repairRequired.insert(prerequisiteId);
                pending.push_back(prerequisiteId);
            }
        }
        for (std::vector<std::string> alternatives : step.prerequisiteAnyOf)
        {
            bool const satisfied = std::any_of(alternatives.begin(), alternatives.end(), [&](std::string const& id) {
                return IsSatisfied(synchronization.progress, steps_[indexById_.at(id)]);
            });
            if (!satisfied)
            {
                std::sort(alternatives.begin(), alternatives.end());
                alternativeGroups.insert(std::move(alternatives));
            }
        }
    }

    synchronization.repairRequiredStepIds.assign(repairRequired.begin(), repairRequired.end());
    synchronization.repairAlternativeGroups.assign(alternativeGroups.begin(), alternativeGroups.end());
    for (std::string const& stableId : synchronization.repairRequiredStepIds)
    {
        GuideStep const& step = steps_[indexById_.at(stableId)];
        bool const hardReady = std::all_of(step.prerequisites.begin(), step.prerequisites.end(),
            [&](std::string const& prerequisiteId) {
                return IsSatisfied(synchronization.progress, steps_[indexById_.at(prerequisiteId)]);
            });
        bool const alternativesReady = std::all_of(step.prerequisiteAnyOf.begin(), step.prerequisiteAnyOf.end(),
            [&](std::vector<std::string> const& alternatives) {
                return std::any_of(alternatives.begin(), alternatives.end(), [&](std::string const& id) {
                    return IsSatisfied(synchronization.progress, steps_[indexById_.at(id)]);
                });
            });
        if (hardReady && alternativesReady)
            synchronization.repairReadyStepIds.push_back(stableId);
    }

    RouteResolution const synchronizedValidation = Resolve(synchronization.progress);
    synchronization.errors = synchronizedValidation.progressErrors;
    return synchronization;
}

std::vector<GuideStep> const& GuideRoute::Steps() const noexcept
{
    return steps_;
}

RouteIdentity const& GuideRoute::Identity() const noexcept
{
    return identity_;
}

void GuideRoute::Validate() const
{
    std::vector<std::size_t> indegree(steps_.size(), 0);
    std::vector<std::vector<std::size_t>> dependents(steps_.size());
    std::map<std::string, std::vector<std::string>> exclusiveGroups;
    for (GuideStep const& step : steps_)
    {
        if (step.prerequisites.size() > MaximumPrerequisitesPerStep)
            throw std::invalid_argument("guide step exceeds the maximum prerequisite count: " + step.stableId);
        if (step.skippedSatisfiesDependents && !step.maySkip)
            throw std::invalid_argument("non-skippable step cannot satisfy dependents when skipped: " + step.stableId);
        if (step.prerequisiteAnyOf.size() > MaximumAlternativeGroupsPerStep)
            throw std::invalid_argument("guide step exceeds the maximum alternative prerequisite groups: " + step.stableId);
        if (step.exclusiveGroup.has_value())
        {
            if (!IsValidStableId(*step.exclusiveGroup))
                throw std::invalid_argument("invalid exclusive group for " + step.stableId);
            exclusiveGroups[*step.exclusiveGroup].push_back(step.stableId);
        }
        if (step.clusterId.has_value() && !IsValidStableId(*step.clusterId))
            throw std::invalid_argument("invalid objective cluster for " + step.stableId);

        std::set<std::pair<std::uint8_t, std::uint32_t>> seenContentRefs;
        for (ContentRef const& contentRef : step.contentRefs)
        {
            if (!IsValidContentKind(contentRef.kind) ||
                (contentRef.id == 0 && contentRef.kind != ContentKind::Map) ||
                !seenContentRefs.emplace(static_cast<std::uint8_t>(contentRef.kind), contentRef.id).second)
                throw std::invalid_argument("invalid or duplicate typed content reference for " + step.stableId);
        }

        std::set<std::string> seenPrerequisites;
        for (std::string const& prerequisite : step.prerequisites)
        {
            if (!seenPrerequisites.insert(prerequisite).second)
                throw std::invalid_argument("duplicate prerequisite " + prerequisite + " for " + step.stableId);
            if (!indexById_.contains(prerequisite))
                throw std::invalid_argument("missing prerequisite " + prerequisite + " for " + step.stableId);
            if (prerequisite == step.stableId)
                throw std::invalid_argument("guide step cannot require itself: " + step.stableId);

            std::size_t const stepIndex = indexById_.at(step.stableId);
            std::size_t const prerequisiteIndex = indexById_.at(prerequisite);
            ++indegree[stepIndex];
            dependents[prerequisiteIndex].push_back(stepIndex);
        }
        for (std::vector<std::string> const& alternatives : step.prerequisiteAnyOf)
        {
            if (alternatives.empty() || alternatives.size() > MaximumPrerequisitesPerStep)
                throw std::invalid_argument("invalid alternative prerequisite group for " + step.stableId);
            std::set<std::string> seenAlternatives;
            for (std::string const& prerequisite : alternatives)
            {
                if (!seenAlternatives.insert(prerequisite).second)
                    throw std::invalid_argument("duplicate alternative prerequisite " + prerequisite + " for " + step.stableId);
                if (!indexById_.contains(prerequisite))
                    throw std::invalid_argument("missing alternative prerequisite " + prerequisite + " for " + step.stableId);
                if (prerequisite == step.stableId)
                    throw std::invalid_argument("guide step cannot alternatively require itself: " + step.stableId);
                std::size_t const stepIndex = indexById_.at(step.stableId);
                std::size_t const prerequisiteIndex = indexById_.at(prerequisite);
                ++indegree[stepIndex];
                dependents[prerequisiteIndex].push_back(stepIndex);
            }
        }
    }

    for (auto const& [groupId, members] : exclusiveGroups)
    {
        if (members.size() < 2)
            throw std::invalid_argument("exclusive group must contain at least two steps: " + groupId);
    }

    std::deque<std::size_t> ready;
    for (std::size_t index = 0; index < indegree.size(); ++index)
    {
        if (indegree[index] == 0)
            ready.push_back(index);
    }

    std::size_t visited = 0;
    while (!ready.empty())
    {
        std::size_t const current = ready.front();
        ready.pop_front();
        ++visited;
        for (std::size_t const dependent : dependents[current])
        {
            --indegree[dependent];
            if (indegree[dependent] == 0)
                ready.push_back(dependent);
        }
    }
    if (visited != steps_.size())
        throw std::invalid_argument("guide route contains a prerequisite cycle");
}
}
