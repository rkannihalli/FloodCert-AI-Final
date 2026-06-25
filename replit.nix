{pkgs}: {
  deps = [
    pkgs.wget
    pkgs.pass-nodmenu
    pkgs.driversi686Linux.mesa-demos
    pkgs.python313Packages.gdal
    pkgs.mailutils
    pkgs.inshellisense
    pkgs.psmisc
    pkgs.fontconfig
    pkgs.libffi
    pkgs.gdk-pixbuf
    pkgs.cairo
    pkgs.pango
  ];
}
