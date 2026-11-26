// Copy this into your Unity project at: Assets/Editor/CIBuild.cs
//
// Headless entry points for the build server's pre-build steps. These run in
// batchmode via `-executeMethod`, one Unity launch each.
//
// IMPORTANT ORDERING: the definition blob must be built BEFORE the Addressables
// content, so the blob is packaged into the bundles that get uploaded to CCD.
// CIBuild.BuildBlobAndAddressables does both, in that order, in one launch.
//
// The CCD UPLOAD is deliberately NOT done here: the CCD Management SDK does not
// work in batchmode (it can't resolve the cloud project id — Unity confirms the
// SDK is "editor only"). So uploading is a separate shell step using the `ucd`
// CLI, which IS supported for build pipelines. This method just produces the
// bundles on disk; the shell step uploads them.

#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using UnityEditor;
using UnityEditor.AddressableAssets;
using UnityEditor.AddressableAssets.Settings;
using UnityEditor.Build;
using UnityEngine;
using Debug = UnityEngine.Debug;

namespace VoxelMage.EditorTools
{
    public static class CIBuild
    {
        // The Addressables profile to build is taken from the build server
        // (env var UBS_ADDRESSABLES_PROFILE, set per-project in the UI), so this
        // one file works unchanged across every project. Falls back to
        // "Development" if the env var isn't set (e.g. building by hand).
        private static string ProfileName()
        {
            var p = Environment.GetEnvironmentVariable("UBS_ADDRESSABLES_PROFILE");
            return string.IsNullOrEmpty(p) ? "Development" : p;
        }

        // The set of mutually-exclusive auth-domain symbols. When we apply a new
        // one, any of these already present is removed first (same behaviour as
        // your SetAuthDomain menu). Keep this list in sync with SetAuthDomain.
        private static readonly string[] AuthSymbols = {
            "TK_ANDROID_DEV", "TK_ANDROID_PROD", "TK_IOS_DEV", "TK_IOS_PROD",
            "TK_WIN_DEV", "TK_WIN_PROD", "TK_QUEST_DEV", "TK_QUEST_PROD",
        };

        /// <summary>
        /// Pre-build step 0: apply scripting defines from UBS_SCRIPTING_DEFINES
        /// (set per-project in the build UI) to the target being built
        /// (UBS_PLATFORM: iOS or Android). This MUST run as
        /// its OWN batchmode step, before the blob/Addressables/player steps,
        /// because changing defines triggers a recompile — the new code (e.g. the
        /// CCD auth header behind #if TK_IOS_DEV) is only active on the NEXT
        /// Unity launch. Running it separately guarantees that.
        /// Without the right auth define, CCD content requests get HTTP 401.
        /// </summary>
        public static void SetScriptingDefines()
        {
            try
            {
                var raw = Environment.GetEnvironmentVariable("UBS_SCRIPTING_DEFINES") ?? "";
                var wanted = new List<string>();
                foreach (var s in raw.Split(';', ','))
                {
                    var t = s.Trim();
                    if (t.Length > 0) wanted.Add(t);
                }

                // Apply to the platform being built. This used to be hardcoded to
                // iOS, which would silently leave Android builds without their
                // auth define (→ CCD 401s). UBS_PLATFORM is set by the server;
                // fall back to the active target (= -buildTarget) when run by hand.
                var plat = (Environment.GetEnvironmentVariable("UBS_PLATFORM") ?? "").Trim().ToLowerInvariant();
                var target = plat == "android" ? NamedBuildTarget.Android
                           : plat == "ios"     ? NamedBuildTarget.iOS
                           : NamedBuildTarget.FromBuildTargetGroup(EditorUserBuildSettings.selectedBuildTargetGroup);
                var current = PlayerSettings.GetScriptingDefineSymbols(target)
                    .Split(';', StringSplitOptions.RemoveEmptyEntries);

                var result = new List<string>();
                // Keep everything that isn't a (possibly stale) auth symbol we manage.
                foreach (var sym in current)
                    if (!IsManagedAuth(sym)) result.Add(sym.Trim());
                // Add the requested ones (auth or otherwise), de-duped.
                foreach (var w in wanted)
                    if (!result.Contains(w)) result.Add(w);

                var joined = string.Join(";", result);
                PlayerSettings.SetScriptingDefineSymbols(target, joined);
                Debug.Log($"[CIBuild] {target.TargetName} scripting defines set to: {joined}");
                // Persist so the recompile on next launch picks it up.
                AssetDatabase.SaveAssets();
            }
            catch (Exception e)
            {
                Debug.LogError($"[CIBuild] SetScriptingDefines FAILED: {e}");
                EditorApplication.Exit(1);
            }
        }

        private static bool IsManagedAuth(string sym)
        {
            sym = sym.Trim();
            foreach (var a in AuthSymbols) if (a == sym) return true;
            return false;
        }

        /// <summary>
        /// Pre-build step: stamp the server-assigned build number into
        /// PlayerSettings so each build is unique (stores reject duplicates).
        /// Reads UBS_BUILD_NUMBER. Applies to BOTH iOS (buildNumber) and Android
        /// (bundleVersionCode) so the same step works for every target. Does NOT
        /// change the human version string (bundleVersion) — that stays whatever
        /// the project committed. Writes nothing if UBS_BUILD_NUMBER is empty
        /// (e.g. install/distribute runs).
        /// Run this as its OWN batchmode step (like SetScriptingDefines) so the
        /// value is persisted before the player build launch reads it.
        /// </summary>
        public static void StampVersion()
        {
            try
            {
                var raw = Environment.GetEnvironmentVariable("UBS_BUILD_NUMBER") ?? "";
                raw = raw.Trim();
                if (raw.Length == 0)
                {
                    Debug.Log("[CIBuild] StampVersion: UBS_BUILD_NUMBER empty, leaving build number unchanged.");
                    return;
                }

                // iOS build number is a string; Android bundleVersionCode is int.
                PlayerSettings.iOS.buildNumber = raw;
                if (int.TryParse(raw, out var code))
                    PlayerSettings.Android.bundleVersionCode = code;

                Debug.Log($"[CIBuild] StampVersion: build number set to {raw} "
                          + $"(version {PlayerSettings.bundleVersion}).");
                AssetDatabase.SaveAssets();
            }
            catch (Exception e)
            {
                Debug.LogError($"[CIBuild] StampVersion FAILED: {e}");
                EditorApplication.Exit(1);
            }
        }

        /// <summary>
        /// Pre-build step 1: build the definition blob, then build Addressables
        /// content for the CCD profile. Exits non-zero on any failure so the
        /// build server aborts before shipping an app with missing content.
        /// </summary>
        public static void BuildBlobAndAddressables()
        {
            try
            {
                BuildBlob();
                BuildAddressables();
                Debug.Log("[CIBuild] Blob + Addressables build complete.");
            }
            catch (Exception e)
            {
                Debug.LogError($"[CIBuild] FAILED: {e}");
                EditorApplication.Exit(1);
            }
        }

        private static void BuildBlob()
        {
            Debug.Log("[CIBuild] Building definition blob...");
            // Calls your existing headless blob builder (from
            // DefinitionBlobBuilderWindow). Uses its defaults, which write the
            // blob into Assets/GameData/ where Addressables will pick it up.
            var report = VoxelMage.EditorTools.DefinitionBlobBuilderWindow.BuildHeadless();
            Debug.Log("[CIBuild] Blob report:\n" + report);
            // Make sure the written blob assets are imported before Addressables
            // builds, or the fresh bytes won't be in the bundles.
            AssetDatabase.Refresh();
        }

        private static void BuildAddressables()
        {
            var settings = AddressableAssetSettingsDefaultObject.Settings;
            if (settings == null)
                throw new Exception("No AddressableAssetSettings found. Open the "
                    + "Addressables Groups window once to create them.");

            // Select the profile the CCD bucket is wired to, so bundles build
            // with the correct remote load path.
            string profileName = ProfileName();
            string profileId = settings.profileSettings.GetProfileId(profileName);
            if (string.IsNullOrEmpty(profileId))
                throw new Exception($"Addressables profile '{profileName}' not found. "
                    + "Set the Addressables profile in the project's UI settings.");
            settings.activeProfileId = profileId;
            Debug.Log($"[CIBuild] Addressables profile set to '{profileName}'.");

            Debug.Log("[CIBuild] Building Addressables player content...");
            AddressableAssetSettings.BuildPlayerContent(out var result);
            if (!string.IsNullOrEmpty(result.Error))
                throw new Exception("Addressables build error: " + result.Error);
            Debug.Log($"[CIBuild] Addressables build OK. Output: {result.OutputPath}");
        }
    }
}
#endif