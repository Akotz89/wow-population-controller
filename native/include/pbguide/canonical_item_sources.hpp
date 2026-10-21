#pragma once
#include "pbguide/gear_planner.hpp"
#include <memory>
#include <string_view>
namespace pbguide {
struct SourcePredicateProgram;
enum class ProjectionDomain { OfflineContract, EffectiveRealm };
struct OfflineSourcePins {
    std::string candidateId, generation, graphDigest, manifestDigest, projectionDigest;
    bool operator==(OfflineSourcePins const&) const = default;
};
class EffectiveDatasetBinding {
public:
    // expectedSha256 must come from the authenticated Root/caller binding, never from this document.
    static EffectiveDatasetBinding FromRootAcceptedManifest(std::string_view bytes, std::string_view expectedSha256);
private:
    friend class AcceptedDatasetRegistry;
    friend class CanonicalItemSourceLoader;
    explicit EffectiveDatasetBinding(OfflineSourcePins pins) : pins_(std::move(pins)) {}
    OfflineSourcePins pins_;
};
class AcceptedDatasetRegistry {
public:
    // Deliberately empty until a source-pinned frozen extract receives acceptance.
    static std::optional<EffectiveDatasetBinding> Lookup(std::string_view candidateId);
};
class LoadedItemSources {
    friend class CanonicalItemSourceLoader;
    friend class CanonicalGearPlanningService;
    LoadedItemSources(ItemSourceIndex index, OfflineSourcePins pins, ProjectionDomain domain,
        std::map<std::string,std::string> blocked, std::string core, std::string playerbots,
        std::shared_ptr<SourcePredicateProgram const> predicates)
        : coreRevision_(std::move(core)), playerbotsRevision_(std::move(playerbots)), index_(std::move(index)), pins_(std::move(pins)), domain_(domain), blocked_(std::move(blocked)), predicates_(std::move(predicates)) {}
    std::string coreRevision_, playerbotsRevision_;
    ItemSourceIndex index_;
    OfflineSourcePins pins_;
    ProjectionDomain domain_;
    std::map<std::string,std::string> blocked_;
    std::shared_ptr<SourcePredicateProgram const> predicates_;
public:
    std::string const& CoreRevision() const noexcept { return coreRevision_; }
    std::string const& PlayerbotsRevision() const noexcept { return playerbotsRevision_; }
    ItemSourceIndex const& Index() const noexcept { return index_; }
    OfflineSourcePins const& Pins() const noexcept { return pins_; }
    ProjectionDomain Domain() const noexcept { return domain_; }
    std::map<std::string,std::string> const& BlockedReasons() const noexcept { return blocked_; }
};
class CanonicalItemSourceLoader {
public:
    // projectionBytes is an array of item-source-v1 documents. Source ID explicitly equals edge ID.
    static LoadedItemSources LoadOffline(std::string_view graphBytes, std::string_view manifestBytes,
        std::string_view projectionBytes, OfflineSourcePins const& pins);
    static LoadedItemSources LoadEffectiveRealm(std::string_view graphBytes, std::string_view manifestBytes,
        std::string_view projectionBytes, EffectiveDatasetBinding const& binding);
private:
    static LoadedItemSources Load(std::string_view, std::string_view, std::string_view,
        OfflineSourcePins const&, ProjectionDomain);
};
struct CanonicalGearPlanResult {
    ProjectionDomain domain;
    OfflineSourcePins provenance;
    std::map<std::string,std::string> blockedReasons;
    GearPlan plan;
    bool operator==(CanonicalGearPlanResult const&) const = default;
};
class CanonicalGearPlanningService {
public:
    static std::string ActorQueries(LoadedItemSources const& loaded,
        std::vector<ItemUpgradeTarget> const& targets);
    static CanonicalGearPlanResult PlanEffectiveRealm(LoadedItemSources const& loaded,
        BotProgressSnapshot const& snapshot, CharacterResourceSnapshot const& resources,
        std::vector<ItemUpgradeTarget> const& targets, GearScoringPolicy policy = {},
        std::string_view actorContextJson = {});
    // Concrete validated capsule is mandatory; raw indexes/flags cannot enter this path.
    static CanonicalGearPlanResult PlanOffline(LoadedItemSources const& loaded,
        BotProgressSnapshot const& snapshot, CharacterResourceSnapshot const& resources,
        std::vector<ItemUpgradeTarget> const& targets, GearScoringPolicy policy = {},
        std::string_view actorContextJson = {});
};
}
