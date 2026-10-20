const platformDevices = {
  "NXP595/Apollo4/Apollo5": [
    "stuttgart",
    "galaxy",
    "toulouse",
    "toulouseh",
    "andes",
    "andesw",
    "berlin",
    "cheetah",
    "swordfish",
    "swift",
    "monaco",
    "vienna",
    "vienna2025",
    "meru",
    "fuji",
  ],
  "MHS003": [
    "pike",
    "warsaw",
    "windermere",
    "cologne",
    "geneva",
    "lyon",
    "makalu",
    "matterhorn",
    "rimo",
    "rocky",
    "milan",
    "milan_64m",
    "pamir",
    "pamir_64m",
    "rome_64m",
    "seattle",
  ],
  "MHS003S": ["oslo", "munich", "dublin", "atlas"],
};

const platformJenkinsJobUrl = {
  "NXP595/Apollo4/Apollo5":
    "https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS/",
  "MHS003":
    "https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS_multi_platform/",
  "MHS003S":
    "https://jenkins.huami.com/job/firmware_auto_trigger/job/HuamiOS_multi_platform/",
};

// 平台 → PLATFORM_SELECT（与 platformJenkinsJobUrl 一一对应）。
// MHS003 → mhs003、MHS003S → mhs003s，都走 HuamiOS_multi_platform；NXP 不传（空字符串）。
const platformSelectByPlatform = {
  "NXP595/Apollo4/Apollo5": "",
  "MHS003": "mhs003",
  "MHS003S": "mhs003s",
};

const objectBranch = {
  "stuttgart": [
    "releases/stuttgart/ota"
  ],
  "toulouse": [
    "dev/toulouseh"
  ],
  "toulouseh": [
    "dev/toulouseh",
    "releases/toulouseh/pvt"
  ],
  "windermere": [
    "releases/col_win/ota"
  ],
  "pamir": [
    "releases/pamir32/ota3"
  ],
  "pike": [
    "releases/pike/ota"
  ],
  "cologne": [
    "releases/col_win/ota"
  ],
  "milan_64m": [
    "releases/mpr64/ota4"
  ],
  "pamir_64m": [
    "releases/mpr64/ota4"
  ],
  "milan": [
    "releases/milan32"
  ],
  "geneva": [
    "releases/geneva/pvt"
  ]
}

const template_node_token = {
  windermere: "F1WJwadcvicnq9km4zqckukznhg",
  geneva: "RzHFw5ipYiU5ibkMxSfcgUfDnve",
  cologne: "QH7rwSCUqirpuJkedU9cLSXPnu7",
  pike: "RGiCwSyb2iu8rOkVlIVcgfgPnEb",
  stuttgart: "BxspwrmF2iyi1ckrTvLcbvWanmb",
  toulouse: "FEl1wKpdAifc7tkYU8wcjbLEnub",
  toulouseh: "NNaww14OPiYXBmknxRvcEd0tnFD",
  rome: "QPs3wSUHwinJDxk66O8cnNqXnE6",
  pamir: "QH7rwSCUqirpuJkedU9cLSXPnu7",
  milan_64m: "WxDvwNErQilq4GkYC4hctB1Kndd",
  pamir_64m: "WxDvwNErQilq4GkYC4hctB1Kndd",
  milan: "WxDvwNErQilq4GkYC4hctB1Kndd",
};
