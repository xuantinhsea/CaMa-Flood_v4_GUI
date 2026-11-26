CaMa_Floodcast 1.0 -- example projects
=====================================

Four complete, already-run CaMa-Flood projects for CaMa_Floodcast, with their river maps and
runoff forcing. All use the 15-arcminute (0.25 deg) routing grid and the 1-degree daily runoff
forcing in inp\test_1deg (years 2000-2001).

  projects\mekong       Mekong basin, year 2000, 5,528 river cells -- 4 cases:
                        baseline, no_bifurcation, with_dams, with_levee
  projects\vietnam      Vietnam (national) -- a deliberately truncated cut (Gate 1 demo)
  projects\philippines  Philippines (national) -- a hydrologically closed cut
  projects\cagayan      Cagayan River basin -- a small catchment-scale cut
  map\                  the regional river maps these projects use
                        (map\mekong_15min includes the 1-arcmin downscaling data)
  inp\                  the shared 1-degree daily runoff forcing

HOW TO USE
1. Unzip anywhere you can write to (e.g. C:\CaMa_Floodcast_examples), keeping projects\, map\
   and inp\ side by side -- the projects refer to their maps and forcing by relative path.
   Avoid OneDrive-synchronised folders: re-running a case rewrites hundreds of MB of results.
2. Start CaMa_Floodcast and choose File -> Open project..., then pick a folder under projects\
   (the one containing project.json). Every case already has results, so Map & Results and
   Analysis work immediately.
3. To re-run a case, the program needs the CaMa-Flood model (MAIN_cmf.exe). The installed
   program finds its own copy automatically; when running from source, set it once with
   Simulation -> Model binary.

The CaMa_Floodcast Windows installer contains these same projects; this zip is for running
from source or for adding the examples to an existing installation.

See USER_MANUAL.md / .docx / .pdf, section 4 "Example projects", for a guided tour.

Developed by Nguyen Xuan TINH (Ph.D.)
(c) 2026 Nippon Koei Co.,Ltd. Water Resources & Energy Dept.
The river maps are derived from the CaMa-Flood global map; CaMa-Flood is distributed under the
Apache License 2.0.
