// Template player for a 3D Unity game, scaffolded by Builders Gate.
//
// A CharacterController walker: move on the ground plane, face the way you
// move, jump with coyote time and a buffer, fall faster than you rise. Every
// feel tunable is a public field AND is emitted as telemetry.
//
// Compiles under either input backend: the Input System package when the
// project has switched to it, the legacy Input Manager otherwise.

using UnityEngine;
#if ENABLE_INPUT_SYSTEM && !ENABLE_LEGACY_INPUT_MANAGER
using UnityEngine.InputSystem;
#endif

[RequireComponent(typeof(CharacterController))]
public class PlayerController : MonoBehaviour
{
    public float speed = 6f;
    public float turnSpeed = 720f;
    public float jumpVelocity = 8.5f;
    public float gravity = -20f;
    public float fallMultiplier = 1.6f;
    public float coyoteTime = 0.10f;
    public float jumpBuffer = 0.10f;

    CharacterController _cc;
    float _vy;
    float _coyote;
    float _buffer;
    bool _wasGrounded;
    float _airStart;
    float _startY;
    float _peakY;

    void Awake()
    {
        _cc = GetComponent<CharacterController>();
        // What a playtest bot may press (BGateAgent). Inert without a bot.
        BGate.BGateInput.Declare("move_forward", "move_back", "move_left", "move_right", "jump");
    }

    void Update()
    {
        float dt = Time.deltaTime;
        bool grounded = _cc.isGrounded;
        _coyote = grounded ? coyoteTime : Mathf.Max(0f, _coyote - dt);
        if (JumpPressed()) _buffer = jumpBuffer;
        else _buffer = Mathf.Max(0f, _buffer - dt);

        var input = Move();
        var move = new Vector3(input.x, 0f, input.y);
        if (move.sqrMagnitude > 1f) move.Normalize();
        if (move.sqrMagnitude > 0.0001f)
        {
            var face = Quaternion.LookRotation(move, Vector3.up);
            transform.rotation = Quaternion.RotateTowards(transform.rotation, face, turnSpeed * dt);
        }

        if (grounded && _vy < 0f) _vy = -2f;   // keep isGrounded honest on slopes
        if (_buffer > 0f && _coyote > 0f)
        {
            _vy = jumpVelocity;
            _buffer = 0f;
            _coyote = 0f;
            _airStart = Time.time;
            _startY = _peakY = transform.position.y;
            BGate.BGateTelemetry.Emit("jump", ("jump_velocity", jumpVelocity),
                ("fall_multiplier", fallMultiplier), ("from_coyote", !grounded));
        }
        _vy += gravity * (_vy < 0f ? fallMultiplier : 1f) * dt;

        _cc.Move((move * speed + Vector3.up * _vy) * dt);

        bool now = _cc.isGrounded;
        if (!now) _peakY = Mathf.Max(_peakY, transform.position.y);
        if (now && !_wasGrounded && _airStart > 0f)
        {
            BGate.BGateTelemetry.Emit("land", ("air_time", Time.time - _airStart),
                ("peak_height", _peakY - _startY));
            _airStart = 0f;
        }
        _wasGrounded = now;
    }

    static Vector2 Move()
    {
        float x = 0f, y = 0f;
        if (BGate.BGateInput.Held("move_left")) x -= 1f;
        if (BGate.BGateInput.Held("move_right")) x += 1f;
        if (BGate.BGateInput.Held("move_back")) y -= 1f;
        if (BGate.BGateInput.Held("move_forward")) y += 1f;
#if ENABLE_INPUT_SYSTEM && !ENABLE_LEGACY_INPUT_MANAGER
        var k = Keyboard.current;
        if (k != null)
        {
            if (k.aKey.isPressed || k.leftArrowKey.isPressed) x -= 1f;
            if (k.dKey.isPressed || k.rightArrowKey.isPressed) x += 1f;
            if (k.sKey.isPressed || k.downArrowKey.isPressed) y -= 1f;
            if (k.wKey.isPressed || k.upArrowKey.isPressed) y += 1f;
        }
#else
        x += Input.GetAxisRaw("Horizontal");
        y += Input.GetAxisRaw("Vertical");
#endif
        return new Vector2(Mathf.Clamp(x, -1f, 1f), Mathf.Clamp(y, -1f, 1f));
    }

    static bool JumpPressed()
    {
        if (BGate.BGateInput.Down("jump")) return true;
#if ENABLE_INPUT_SYSTEM && !ENABLE_LEGACY_INPUT_MANAGER
        var k = Keyboard.current;
        return k != null && k.spaceKey.wasPressedThisFrame;
#else
        return Input.GetButtonDown("Jump");
#endif
    }
}
