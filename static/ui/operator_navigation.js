const navigation = document.querySelector(".operator-navigation");
try {
    const preference = "cueporchWorkspaceNavigation";
    navigation.open = localStorage.getItem(preference) !== "collapsed";
    navigation.addEventListener("toggle", () => {
        localStorage.setItem(preference, navigation.open ? "expanded" : "collapsed");
    });
}
catch {
    // The native disclosure still works when storage is blocked.
}
export {};
