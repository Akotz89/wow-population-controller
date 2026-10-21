#pragma once

#include "pbguide/gear_planner.hpp"

#include <string>
#include <string_view>
#include <vector>

namespace pbguide
{
[[nodiscard]] std::vector<std::string> ValidateGearPlanForPersistence(GearPlan const& plan);
[[nodiscard]] GearPlan GearPlanFromJson(std::string_view bytes);
[[nodiscard]] std::string GearPlanToJson(GearPlan const& plan);
}
