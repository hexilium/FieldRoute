-- Retain the upstream access rules, but choose the shortest permitted path.
-- Application travel time is distance / the engineer's fixed speed.
package.path = '/opt/?.lua;' .. package.path
local base = dofile('/opt/' .. os.getenv('OSRM_PROFILE') .. '.lua')
local original_setup = base.setup
local original_turn = base.process_turn
base.setup = function()
  local profile = original_setup()
  profile.properties.weight_name = 'distance'
  profile.properties.continue_straight_at_waypoint = false
  return profile
end
base.process_turn = function(profile, turn)
  original_turn(profile, turn)
  turn.weight = 0
end
return base
