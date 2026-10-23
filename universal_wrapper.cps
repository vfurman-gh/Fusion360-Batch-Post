/**
 * Universal Post Processor Wrapper for Fusion 360 Batch Post
 * 
 * Dynamically wraps ANY Autodesk machine post processor (Lathe or Mill),
 * intercepting onLinear moves authorized by the IPW simulation engine
 * and natively routing them through onRapid().
 */

// 1. Resolve Target Base Post Path
var basePostPath = "";
try {
  basePostPath = getEnvironmentVariable("BASE_POST");
} catch (e) {}
if (!basePostPath && (typeof properties !== "undefined") && properties.basePost) {
  basePostPath = getProperty("basePost");
}
if (!basePostPath) {
  error("Universal Wrapper Error: BASE_POST environment variable or property is not set.");
}

// Include and execute the target machine post configuration
include(basePostPath);

// 2. Load Promoted Air Vectors JSON
var authorizedMap = {};
var authJsonPath = "";
try {
  authJsonPath = getEnvironmentVariable("PROMOTED_MOVES_JSON");
} catch (e) {}
if (!authJsonPath && (typeof properties !== "undefined") && properties.promotedJson) {
  authJsonPath = getProperty("promotedJson");
}

if (authJsonPath && FileSystem.isFile(authJsonPath)) {
  try {
    var rawJson = loadText(authJsonPath, "utf-8");
    authorizedMap = JSON.parse(rawJson);
  } catch (e) {
    error("Universal Wrapper: Failed to parse PROMOTED_MOVES_JSON: " + e.toString());
  }
}

// 3. Coordinate Key Generator (Normalized to 0.0001 mm resolution)
function formatCoord(val) {
  var mmVal = (typeof unit !== "undefined" && unit == IN) ? (val * 25.4) : val;
  return Math.round(mmVal * 10000);
}

function makeVectorKey(secId, x1, y1, z1, x2, y2, z2) {
  return secId + ":" + formatCoord(x1) + "," + formatCoord(y1) + "," + formatCoord(z1) +
         "->" + formatCoord(x2) + "," + formatCoord(y2) + "," + formatCoord(z2);
}

// 4. Track Canned Cycles
var cycleActive = false;
var baseOnCycle = (typeof onCycle !== "undefined") ? onCycle : null;
onCycle = function() {
  cycleActive = true;
  if (baseOnCycle) {
    baseOnCycle();
  }
};
var baseOnCycleEnd = (typeof onCycleEnd !== "undefined") ? onCycleEnd : null;
onCycleEnd = function() {
  cycleActive = false;
  if (baseOnCycleEnd) {
    baseOnCycleEnd();
  }
};

// 5. Hook onLinear to Promote Validated Air Moves
var baseOnLinear = onLinear;
var baseOnRapid = onRapid;
var promotedCount = 0;

onLinear = function(_x, _y, _z, feed) {
  // Guard 1: Never promote moves inside canned/drill cycles
  if (cycleActive) {
    baseOnLinear(_x, _y, _z, feed);
    return;
  }

  var start = getCurrentPosition();
  var secId = currentSection.getId();
  var key = makeVectorKey(secId, start.x, start.y, start.z, _x, _y, _z);

  var entry = authorizedMap[key];

  // Case A: Ordinary cutting move (95% of moves) - zero allocations, instant exit
  if (!entry) {
    baseOnLinear(_x, _y, _z, feed);
    return;
  }

  // Case B: Pure air move (minOccur not specified or <= 1)
  if (!entry.minOccur || entry.minOccur <= 1) {
    promotedCount++;
    writeComment("WRAPPER PROMOTED G0: " + key);
    baseOnRapid(_x, _y, _z);
    return;
  }

  // Case C: Transition vector (cutting on occurrence 1..K-1, air on >= K)
  entry.seen = (entry.seen || 0) + 1;
  if (entry.seen >= entry.minOccur) {
    promotedCount++;
    writeComment("WRAPPER PROMOTED G0 (OCCURRENCE " + entry.seen + "): " + key);
    baseOnRapid(_x, _y, _z);
  } else {
    baseOnLinear(_x, _y, _z, feed);
  }
};

// 5. Hook onClose to report total promotions
var baseOnClose = onClose;
onClose = function() {
  if (baseOnClose) {
    baseOnClose();
  }
  writeComment("UNIVERSAL WRAPPER: RESTORED " + promotedCount + " G0 RAPID MOVES");
};
