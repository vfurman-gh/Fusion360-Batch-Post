/**
 * APT Extractor & Intermediate Compact-NCI Capture Wrapper.
 *
 * Dynamically wraps Autodesk's standard apt.cps to output an ISO 4343 .apt toolpath,
 * while intercepting getIntermediatePath() in onClose() to grab the raw binary
 * Compact-NCI (.cnc) file in a single Fusion CAM post invocation.
 */

description = "APT Extractor & Compact-NCI Capture Wrapper";
vendor = "Paterson Technology / Universal";
legal = "Copyright (C) 2026. All rights reserved.";
certificationLevel = 2;
minimumRevision = 45000;

capabilities = CAPABILITY_TURNING | CAPABILITY_MILLING;
extension = "apt";

var systemAptPath = "";
try {
  systemAptPath = getEnvironmentVariable("SYSTEM_APT_CPS");
} catch (e) {}

if (!systemAptPath || !FileSystem.isFile(systemAptPath)) {
  var candidates = [
    "/Users/vadimf/Documents/Fusion360-Batch-Post-master/apt.cps",
    "/Users/vadimf/Library/Application Support/Autodesk/Fusion 360 CAM/Posts/apt.cps",
    "/Users/vadimf/Library/Application Support/Autodesk/webdeploy/production/c139e2731e42a0340acc3d34de2cbdb01237a27c/Autodesk Fusion.app/Contents/Libraries/Applications/CAM360/Data/Posts/apt.cps"
  ];
  try {
    var cfgFolder = FileSystem.getFolderPath(getConfigurationPath());
    if (cfgFolder) {
      candidates.unshift(FileSystem.getCombinedPath(cfgFolder, "apt.cps"));
    }
  } catch (e) {}

  for (var i = 0; i < candidates.length; ++i) {
    var c = candidates[i];
    if (c && FileSystem.isFile(c) && (c.indexOf("/") === 0 || c.indexOf(":") === 1)) {
      systemAptPath = c;
      break;
    }
  }
}

if (!systemAptPath) {
  systemAptPath = "apt.cps";
}

// Dynamically include Autodesk system apt.cps
include(systemAptPath);

// Hook onClose to capture the intermediate Compact-NCI file
var baseOnClose = (typeof onClose === "function") ? onClose : null;

onClose = function() {
  if (baseOnClose) {
    baseOnClose();
  }

  var targetCnc = getEnvironmentVariable("TARGET_INTERMEDIATE_CNC");
  if (!targetCnc) {
    try {
      var outPath = getOutputPath();
      if (outPath) {
        targetCnc = outPath + ".intermediate.cnc";
      }
    } catch (ignored) {}
  }

  if (targetCnc) {
    try {
      var intermediateSrc = getIntermediatePath();
      if (intermediateSrc && FileSystem.isFile(intermediateSrc)) {
        if (FileSystem.isFile(targetCnc)) {
          try {
            FileSystem.remove(targetCnc);
          } catch (ignored) {}
        }
        FileSystem.copyFile(intermediateSrc, targetCnc);
      }
    } catch (e) {
      writeComment("APT EXTRACTOR ERROR: " + e.toString());
    }
  }
};
