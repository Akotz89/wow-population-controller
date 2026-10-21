#pragma once
#include "pbguide/canonical_item_sources.hpp"
#include <stdexcept>
#include <string>
#include <string_view>
namespace pbguide {
// Codes distinguish absent, invalid, unsafe, failed-before-commit and uncertain
// publication. A digest detects corruption; it does not authenticate a writer.
class GearPlanStoreError : public std::runtime_error {
    std::string code_;
public:
    explicit GearPlanStoreError(std::string code, std::string cause = {})
        : std::runtime_error("gear_plan_store:" + code + (cause.empty() ? "" : ":" + cause.substr(0, 512))), code_(std::move(code)) {}
    std::string_view Code() const noexcept { return code_; }
};
// One private local owner, no concurrent-writer or native-execution authority.
// The absolute directory must already exist with current-UID ownership and 0700.
class OfflineGearPlanStore {
    std::string directory_;
public:
    explicit OfflineGearPlanStore(std::string absoluteDirectory);
    [[nodiscard]] CanonicalGearPlanResult PlanAndSave(LoadedItemSources const& loaded,
        BotProgressSnapshot const& snapshot, CharacterResourceSnapshot const& resources,
        std::vector<ItemUpgradeTarget> const& targets, GearScoringPolicy policy = {}) const;
    // Source-byte entrypoint runs the real loader and planner before committing.
    [[nodiscard]] CanonicalGearPlanResult PlanAndSaveOfflineSources(std::string_view graphBytes,
        std::string_view manifestBytes, std::string_view projectionBytes, OfflineSourcePins const& pins,
        BotProgressSnapshot const& snapshot, CharacterResourceSnapshot const& resources,
        std::vector<ItemUpgradeTarget> const& targets, GearScoringPolicy policy = {}) const;
    void Save(CanonicalGearPlanResult const& result, LoadedItemSources const& loaded,
        BotProgressSnapshot const& snapshot, GearScoringPolicy policy = {}) const;
    // Returns a historical identity-bound offline decision, not current execution
    // authority. Revalidate present native state or replan before executing it.
    [[nodiscard]] CanonicalGearPlanResult Load(std::string_view planId,
        LoadedItemSources const& loaded, BotProgressSnapshot const& snapshot,
        GearScoringPolicy policy = {}) const;
};
}
