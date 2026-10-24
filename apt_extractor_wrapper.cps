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

var systemAptPath = getEnvironmentVariable("SYSTEM_APT_CPS");
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
