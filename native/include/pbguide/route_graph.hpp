#pragma once

#include "pbguide/director.hpp"

#include <map>
#include <set>
#include <string>
#include <vector>

namespace pbguide
{
enum class ContentKind : std::uint8_t
{
    Quest,
    Item,
    Creature,
    GameObject,
    Spell,
    Trainer,
    Vendor,
    TaxiNode,
    Area,
    Map,
    Dungeon,
    LootTemplate,
    Recipe
};

struct ContentRef
{
    ContentKind kind = ContentKind::Quest;
    std::uint32_t id = 0;
};

struct GuideStep
{
    GuideStep(
        std::string stableId,
        ObjectiveCandidate objective,
        std::vector<std::string> prerequisites = {},
        bool maySkip = false,
        bool skippedSatisfiesDependents = false,
        std::vector<ContentRef> contentRefs = {});

    std::string stableId;
    ObjectiveCandidate objective;
    std::vector<std::string> prerequisites;
    bool maySkip = false;
    bool skippedSatisfiesDependents = false;
    std::vector<ContentRef> contentRefs;
    std::vector<std::vector<std::string>> prerequisiteAnyOf;
    std::optional<std::string> exclusiveGroup;
    std::optional<std::string> clusterId;
};

struct RouteIdentity
{
    std::string routeId;
    std::string revision;
    std::string catalogueDigest;
};

struct GuideProgress
{
    RouteIdentity identity;
    std::set<std::string> completed;
    std::set<std::string> skipped;
};

struct RouteResolution
{
    std::vector<ObjectiveCandidate> ready;
    std::map<std::string, std::string> readyClusterByObjective;
    std::map<std::string, std::vector<std::string>> blockedReasons;
    std::vector<std::string> progressErrors;
};

struct CharacterQuestHistory
{
    std::set<std::uint32_t> rewardedQuestIds;
    std::set<std::uint32_t> activeQuestIds;
};

struct RouteSynchronization
{
    GuideProgress progress;
    bool rebuiltFromAuthoritativeHistory = false;
    std::vector<std::string> repairRequiredStepIds;
    std::vector<std::string> repairReadyStepIds;
    std::vector<std::vector<std::string>> repairAlternativeGroups;
    std::vector<std::string> errors;
};

class GuideRoute
{
public:
    GuideRoute(RouteIdentity identity, std::vector<GuideStep> steps);

    [[nodiscard]] RouteResolution Resolve(GuideProgress const& progress) const;
    [[nodiscard]] RouteSynchronization Synchronize(
        GuideProgress const* persisted,
        CharacterQuestHistory const& history) const;
    [[nodiscard]] std::vector<GuideStep> const& Steps() const noexcept;
    [[nodiscard]] RouteIdentity const& Identity() const noexcept;

private:
    void Validate() const;

    RouteIdentity identity_;
    std::vector<GuideStep> steps_;
    std::map<std::string, std::size_t> indexById_;
};
}
