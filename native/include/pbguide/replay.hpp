#pragma once

#include "pbguide/director.hpp"

#include <string>
#include <string_view>
#include <vector>

namespace pbguide
{
struct ExpectedEvaluation
{
    std::string stableId;
    bool eligible = false;
    ScoreBreakdown score;
    std::int64_t retryAtSeconds = 0;
    std::vector<std::string> rejectionReasons;
};

struct ReplayExpectation
{
    PlanDisposition disposition = PlanDisposition::DeferExistingControl;
    std::optional<std::string> selectedStableId;
    std::vector<ExpectedEvaluation> evaluations;
};

struct ReplayCase
{
    std::string schemaVersion;
    std::string catalogueDigest;
    ScoringPolicy policy;
    BotProgressSnapshot snapshot;
    std::vector<ObjectiveCandidate> candidates;
    std::vector<FailureEvidence> failures;
    ReplayExpectation expected;
};

[[nodiscard]] BotProgressSnapshot ParseGearSnapshot(std::string_view jsonText);
[[nodiscard]] ReplayCase ParseReplayCase(std::string_view jsonText);
[[nodiscard]] std::vector<std::string> VerifyReplay(ReplayCase const& replay);
[[nodiscard]] std::string DecisionToJson(PlanDecision const& decision);
}
